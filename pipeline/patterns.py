"""What winning DraftKings NBA lineups look like, and why they won.

Data (all verifiable):
  * Actual DK points for every player-game: computed from NBA.com box scores.
  * Historical DK salaries: RotoGuru's daily DraftKings archive (rotoguru1.com),
    a public record kept since 2014. A night is used only if RotoGuru's DK
    points agree with our NBA.com box-score points for >= 95% of players, so a
    bad or misaligned archive page can never enter the analysis.
  * Your own DraftKings contest results (optional): drop the "contest
    standings" CSVs you download from DraftKings into `contest_results/`. They
    contain the real winning lineups and actual ownership.

Public data does not include every winning lineup in every contest. What we
do have is, for every night, the lineups that scored the most possible
(found exactly with an integer program). Large-field GPPs are typically won
by lineups close to these, so they're the large-sample proxy. Real winners
from your contests are analysed separately and shown alongside.

For each top lineup we record its structure (salary used, price tiers, team
and game stacks) and, for each player, the context that explains the score:
a teammate out, a surprise start, a minutes spike, overtime, a blowout, a
high-scoring game, and how they compared with their pre-game projection.

Run daily: `python -m pipeline.patterns` (it only computes new nights).
"""
from __future__ import annotations

import csv
import io
import json
import logging
import re
import sys
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

from . import history, model
from .config import DATA_DIR, DK_SALARY_CAP, ROOT
from .http import session
from .names import norm_name, team_code
from .sim import HALL_NEED, pos_code

log = logging.getLogger(__name__)

ROTOGURU = "http://rotoguru1.com/cgi-bin/hyday.pl?game=dk&mon={m}&day={d}&year={y}&scsv=1"
CACHE = DATA_DIR / "history" / "salaries"
NIGHTS = DATA_DIR / "patterns" / "nights"
OUT = DATA_DIR / "patterns.json"
CONTESTS = ROOT / "contest_results"
TOP_K = 20


# ── salaries ─────────────────────────────────────────────────────────────────
def fetch_salaries(day: date) -> pd.DataFrame:
    path = CACHE / f"{day.isoformat()}.csv"
    if path.exists():
        return pd.read_csv(path)
    r = session().get(ROTOGURU.format(m=day.month, d=day.day, y=day.year), timeout=30)
    r.raise_for_status()
    df = parse_rotoguru(r.text)
    CACHE.mkdir(parents=True, exist_ok=True)
    df.to_csv(path, index=False)
    return df


def parse_rotoguru(page: str) -> pd.DataFrame:
    text = re.sub(r"<[^>]+>", "\n", page)
    lines = [l.strip() for l in text.splitlines() if ";" in l]
    header_i = next((i for i, l in enumerate(lines) if "Name" in l and "Sal" in l), None)
    cols = ["name", "position", "salary", "dk_pts", "team", "opp", "minutes"]
    if header_i is None:
        return pd.DataFrame(columns=cols)
    head = [h.strip().lower() for h in lines[header_i].split(";")]

    def col(*keys):
        for k in keys:
            for i, h in enumerate(head):
                if k in h:
                    return i
        return None

    i_name, i_pos, i_sal = col("name"), col("pos"), col("sal")
    i_pts, i_team, i_opp, i_min = col("pts", "points"), col("team"), col("oppt", "opp"), col("min")
    rows = []
    for l in lines[header_i + 1:]:
        c = [x.strip() for x in l.split(";")]
        if len(c) < len(head) or not c[i_name]:
            continue
        sal = re.sub(r"[^\d]", "", c[i_sal]) if i_sal is not None else ""
        if not sal:
            continue
        name = c[i_name]
        if "," in name:
            last, first = name.split(",", 1)
            name = f"{first.strip()} {last.strip()}"
        try:
            pts = float(c[i_pts]) if i_pts is not None and c[i_pts] not in ("", "DNP", "NA") else np.nan
        except ValueError:
            pts = np.nan
        rows.append({"name": name, "position": (c[i_pos] if i_pos is not None else "").upper(),
                     "salary": int(sal), "dk_pts": pts,
                     "team": team_code(c[i_team]) if i_team is not None else "",
                     "opp": team_code(c[i_opp].replace("@", "").replace("v ", "").strip()) if i_opp is not None else "",
                     "minutes": c[i_min] if i_min is not None else ""})
    return pd.DataFrame(rows, columns=cols)


def verify_night(sal: pd.DataFrame, logs: pd.DataFrame) -> tuple[pd.DataFrame, float]:
    """Join salaries to NBA.com box scores; returns joined rows + agreement rate."""
    if sal.empty or logs.empty:
        return pd.DataFrame(), 0.0
    s = sal.assign(nkey=sal["name"].map(norm_name))
    b = logs.assign(nkey=logs["name"].map(norm_name))
    j = s.merge(b, on=["nkey", "team"], suffixes=("_rg", ""))
    j = j[j["played"]].copy()
    j["position"] = j["position_rg"]   # DraftKings eligibility, not the box-score position
    if j.empty:
        return j, 0.0
    agree = (j["dk_pts"] - j["dk_fp"]).abs() <= 0.26
    checked = j["dk_pts"].notna()
    rate = float(agree[checked].mean()) if checked.any() else 0.0
    return j, rate


# ── exact top-K lineups for a night ──────────────────────────────────────────
def top_lineups(df: pd.DataFrame, k: int = TOP_K) -> list[list[int]]:
    import highspy

    df = df[df["dk_fp"] > 0].reset_index(drop=True)
    P = len(df)
    if P < 8:
        return []
    code = np.array([pos_code(p) for p in df["position"]])
    h = highspy.Highs()
    h.setOptionValue("output_flag", False)
    h.addVars(P, np.zeros(P), np.ones(P))
    h.changeColsIntegrality(P, np.arange(P, dtype=np.int32), np.array([highspy.HighsVarType.kInteger] * P))

    def row(idx, coef, lo, hi):
        h.addRow(lo, hi, len(idx), np.asarray(idx, dtype=np.int32), np.asarray(coef, dtype=float))

    row(np.arange(P), np.ones(P), 8, 8)
    for U, need in HALL_NEED.items():
        if need:
            idx = np.nonzero(code & U)[0]
            row(idx, np.ones(len(idx)), need, highspy.kHighsInf)
    row(np.arange(P), df["salary"].to_numpy(float), 0, DK_SALARY_CAP)
    h.changeColsCost(P, np.arange(P, dtype=np.int32), df["dk_fp"].to_numpy(float))
    h.changeObjectiveSense(highspy.ObjSense.kMaximize)
    out = []
    for _ in range(k):
        h.run()
        if h.getModelStatus() != highspy.HighsModelStatus.kOptimal:
            break
        x = np.asarray(h.getSolution().col_value)
        lu = sorted(int(i) for i in np.nonzero(x > 0.5)[0])
        if len(lu) != 8:
            break
        out.append(lu)
        row(lu, np.ones(8), 0, 7)  # next solve must differ by at least one player
    return out


# ── context: why each player scored ─────────────────────────────────────────
def night_context(day: date, hist_prepared: pd.DataFrame, base: pd.DataFrame) -> pd.DataFrame:
    """Per player-game context for one night (uses only pre-game info for baselines)."""
    tonight = hist_prepared[hist_prepared["d"] == day].copy()
    if tonight.empty:
        return tonight
    b = base[base["d"] == day][["person_id", "game_id", "m_base", "pred"]]
    tonight = tonight.merge(b, on=["person_id", "game_id"], how="left")
    # rotation players who sat (recent minutes >= 24)
    recent = base[base["d"] < day].sort_values("d").groupby("person_id").tail(1)
    regulars = set(recent.loc[recent["m_base"] >= 24, "person_id"])
    sat = tonight[(~tonight["played"]) & tonight["person_id"].isin(regulars)]
    teams_missing = sat.groupby("team")["name"].apply(list).to_dict()
    tonight["teammate_out"] = tonight["team"].map(lambda t: len(teams_missing.get(t, [])) > 0)
    start_rate = (hist_prepared[hist_prepared["d"] < day].sort_values("d").groupby("person_id")["starter"]
                  .apply(lambda s: s.astype(bool).tail(10).mean()))
    tonight["prior_start_rate"] = tonight["person_id"].map(start_rate).fillna(0)
    tonight["surprise_start"] = tonight["starter"].astype(bool) & (tonight["prior_start_rate"] < 0.3)
    tonight["minutes_spike"] = tonight["minutes"] >= tonight["m_base"].fillna(99) + 6
    team_min = tonight.groupby(["game_id", "team"])["minutes"].transform("sum")
    tonight["overtime"] = team_min > 245
    tonight["margin"] = (tonight["team_score"] - tonight["opp_score"]).abs()
    tonight["blowout"] = tonight["margin"] >= 20
    tot = tonight["team_score"] + tonight["opp_score"]
    tonight["high_total"] = tot >= tot.quantile(0.75)
    tonight["beat_proj"] = tonight["dk_fp"] / tonight["pred"].where(tonight["pred"] > 0)
    return tonight


def analyse_night(day: date, joined: pd.DataFrame, ctx: pd.DataFrame) -> dict | None:
    df = joined.merge(ctx[["person_id", "game_id", "teammate_out", "surprise_start", "minutes_spike",
                           "overtime", "blowout", "high_total", "beat_proj", "pred"]],
                      on=["person_id", "game_id"], how="left").reset_index(drop=True)
    df = df[df["dk_fp"] > 0].reset_index(drop=True)
    lus = top_lineups(df)
    if not lus:
        return None
    # projected value rank: was the player an obvious play before the game?
    df["proj_value"] = df["pred"].fillna(0) / (df["salary"] / 1000)
    df["proj_value_rank"] = df["proj_value"].rank(ascending=False)
    lineups = []
    for lu in lus:
        sub = df.iloc[lu]
        team_counts = sub["team"].value_counts()
        game_key = sub.apply(lambda r: "|".join(sorted([r["team"], r["opp"]])), axis=1)
        game_counts = game_key.value_counts()
        both_sides = any(sub.loc[game_key == g, "team"].nunique() == 2 for g in game_counts.index)
        lineups.append({
            "score": round(float(sub["dk_fp"].sum()), 2),
            "salary": int(sub["salary"].sum()),
            "studs": int((sub["salary"] >= 9000).sum()),
            "mid": int(((sub["salary"] >= 5000) & (sub["salary"] < 9000)).sum()),
            "value": int((sub["salary"] < 5000).sum()),
            "punts": int((sub["salary"] <= 3500).sum()),
            "max_team": int(team_counts.max()),
            "games": int(game_counts.size),
            "max_game": int(game_counts.max()),
            "bring_back": bool(both_sides),
            "players": [{
                "name": r["name"], "salary": int(r["salary"]), "pts": round(float(r["dk_fp"]), 1),
                "teammate_out": bool(r.get("teammate_out")), "surprise_start": bool(r.get("surprise_start")),
                "minutes_spike": bool(r.get("minutes_spike")), "overtime": bool(r.get("overtime")),
                "blowout": bool(r.get("blowout")), "high_total": bool(r.get("high_total")),
                "beat_proj": None if pd.isna(r.get("beat_proj")) else round(float(r["beat_proj"]), 2),
                "proj_value_rank": None if pd.isna(r.get("proj_value_rank")) else int(r["proj_value_rank"]),
            } for _, r in sub.iterrows()],
        })
    return {"date": day.isoformat(), "players": int(len(df)), "games": int(df["game_id"].nunique()),
            "lineups": lineups}


# ── your real contest results ────────────────────────────────────────────────
def parse_contest_standings(text: str) -> dict | None:
    """DraftKings contest standings CSV -> winners with real ownership."""
    rows = list(csv.reader(io.StringIO(text)))
    if not rows or "Rank" not in rows[0]:
        return None
    head = rows[0]
    i_rank, i_pts, i_lu = head.index("Rank"), head.index("Points"), head.index("Lineup")
    i_player = head.index("Player") if "Player" in head else None
    i_own = head.index("%Drafted") if "%Drafted" in head else None
    own = {}
    if i_player is not None and i_own is not None:
        for r in rows[1:]:
            if len(r) > i_own and r[i_player]:
                try:
                    own[norm_name(r[i_player])] = float(r[i_own].strip("%"))
                except ValueError:
                    pass
    entries = []
    for r in rows[1:]:
        if len(r) <= i_lu or not r[i_rank].strip():
            continue
        try:
            rank, pts = int(r[i_rank]), float(r[i_pts])
        except ValueError:
            continue
        names = [n.strip() for n in re.split(r"\b(?:PG|SG|SF|PF|C|G|F|UTIL)\b", r[i_lu]) if n.strip()]
        entries.append({"rank": rank, "points": pts, "players": names})
    if not entries:
        return None
    entries.sort(key=lambda e: e["rank"])
    n = len(entries)
    top = entries[: max(1, n // 1000)]
    winners = []
    for e in top:
        o = [own.get(norm_name(p)) for p in e["players"]]
        o = [x for x in o if x is not None]
        winners.append({"rank": e["rank"], "points": e["points"], "players": e["players"],
                        "total_own": round(sum(o), 1) if o else None,
                        "sub10_owned": sum(1 for x in o if x < 10) if o else None})
    return {"entries": n, "winning_score": entries[0]["points"], "top_0_1pct": winners}


# ── summary + generator rules ────────────────────────────────────────────────
def summarise(nights: list[dict], contests: list[dict]) -> dict:
    lus = [lu for n in nights for lu in n["lineups"]]
    players = [p for lu in lus for p in lu["players"]]
    if not lus:
        return {"nights": 0, "rules": default_rules(),
                "note": "No verified historical nights yet; using default rules."}

    def pct(x):
        return round(100.0 * float(np.mean(x)), 1)

    sal = np.array([lu["salary"] for lu in lus])
    max_team = np.array([lu["max_team"] for lu in lus])
    max_game = np.array([lu["max_game"] for lu in lus])
    beat = [p["beat_proj"] for p in players if p["beat_proj"]]
    rank = [p["proj_value_rank"] for p in players if p["proj_value_rank"]]
    summary = {
        "nights": len(nights),
        "lineups_analysed": len(lus),
        "date_range": [nights[0]["date"], nights[-1]["date"]],
        "structure": {
            "salary_used_median": int(np.median(sal)),
            "salary_used_p5": int(np.percentile(sal, 5)),
            "pct_using_49k_plus": pct(sal >= 49000),
            "avg_studs_9k_plus": round(float(np.mean([lu["studs"] for lu in lus])), 2),
            "avg_mid_5k_9k": round(float(np.mean([lu["mid"] for lu in lus])), 2),
            "avg_value_under_5k": round(float(np.mean([lu["value"] for lu in lus])), 2),
            "avg_punts_3_5k_or_less": round(float(np.mean([lu["punts"] for lu in lus])), 2),
            "pct_3plus_from_one_team": pct(max_team >= 3),
            "pct_4plus_from_one_game": pct(max_game >= 4),
            "pct_with_bring_back": pct([lu["bring_back"] for lu in lus]),
            "avg_games_used": round(float(np.mean([lu["games"] for lu in lus])), 2),
        },
        "why_players_hit": {
            "teammate_out_pct": pct([p["teammate_out"] for p in players]),
            "surprise_start_pct": pct([p["surprise_start"] for p in players]),
            "minutes_spike_pct": pct([p["minutes_spike"] for p in players]),
            "overtime_pct": pct([p["overtime"] for p in players]),
            "blowout_game_pct": pct([p["blowout"] for p in players]),
            "high_total_game_pct": pct([p["high_total"] for p in players]),
            "median_points_vs_projection": round(float(np.median(beat)), 2) if beat else None,
            "pct_outside_top20_projected_value": pct([r > 20 for r in rank]) if rank else None,
        },
        "real_contests": {"count": len(contests), "contests": contests[-20:]},
    }
    summary["rules"] = {
        "min_salary": int(max(45000, (np.percentile(sal, 5) // 100) * 100)),
        "max_per_team": int(max(3, np.percentile(max_team, 99))),
        "max_per_game": int(max(4, np.percentile(max_game, 99))),
    }
    return summary


def default_rules() -> dict:
    return {"min_salary": 48500, "max_per_team": 4, "max_per_game": 6}


def load_rules() -> dict:
    try:
        return json.loads(OUT.read_text()).get("rules") or default_rules()
    except (OSError, ValueError):
        return default_rules()


def run(max_new_nights: int = 400) -> dict:
    logs = history.load()
    if logs.empty:
        log.warning("no box-score history yet")
        return {}
    today = date.today()
    prep = model.prepare_history(logs, today)
    from .factors import baseline_predictions
    base = baseline_predictions(prep)
    NIGHTS.mkdir(parents=True, exist_ok=True)
    days = sorted(set(prep["d"]))
    done = {p.stem for p in NIGHTS.glob("*.json")}
    new = 0
    for day in days:
        if day.isoformat() in done or new >= max_new_nights:
            continue
        new += 1
        try:
            sal = fetch_salaries(day)
        except Exception as e:  # noqa: BLE001
            log.info("salaries %s unavailable: %s", day, e)
            continue
        tonight = prep[prep["d"] == day]
        joined, rate = verify_night(sal, tonight)
        rec = {"date": day.isoformat(), "verified": rate >= 0.95, "agreement": round(rate, 3)}
        if rec["verified"]:
            ctx = night_context(day, prep, base)
            res = analyse_night(day, joined, ctx)
            if res:
                rec.update(res)
        (NIGHTS / f"{day.isoformat()}.json").write_text(json.dumps(rec))
    nights = []
    for p in sorted(NIGHTS.glob("*.json")):
        try:
            n = json.loads(p.read_text())
        except ValueError:
            continue
        if n.get("verified") and n.get("lineups"):
            nights.append(n)
    contests = []
    if CONTESTS.exists():
        for f in sorted(CONTESTS.glob("*.csv")):
            res = parse_contest_standings(f.read_text(errors="ignore"))
            if res:
                contests.append({"file": f.name, **res})
    summary = summarise(nights, contests)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(summary, indent=1))
    log.info("patterns: %d verified nights, rules %s", summary.get("nights", 0), summary.get("rules"))
    return summary


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    run(int(sys.argv[1]) if len(sys.argv) > 1 else 400)
