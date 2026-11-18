"""What winning DraftKings NBA lineups look like, and why they won.

Data (all verifiable):
  * Actual DK points for every player-game: computed from NBA.com box scores.
  * Historical DK salaries: RotoGuru's daily DraftKings archive (rotoguru1.com),
    a public record kept since 2014. A night is used only if RotoGuru's DK
    points agree with our NBA.com box-score points for >= 95% of players, so a
    bad or misaligned archive page can never enter the analysis.
  * Your own DraftKings contest results are handled privately by
    pipeline/contests.py and never published here.

Public data does not include every winning lineup in every contest. What we
do have is, for every night, the lineups that scored the most possible
(found exactly with an integer program). Large-field GPPs are typically won
by lineups close to these, so they're the large-sample proxy. Real winners
from your contests are analysed privately (pipeline/contests.py).

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
from .config import DATA_DIR, DK_SALARY_CAP
from .http import session
from .names import norm_name, team_code
from .sim import HALL_NEED, pos_code

log = logging.getLogger(__name__)

ROTOGURU = "http://rotoguru1.com/cgi-bin/hyday.pl?game=dk&mon={m}&day={d}&year={y}&scsv=1"
CACHE = DATA_DIR / "history" / "salaries"
NIGHTS = DATA_DIR / "patterns" / "nights"
OUT = DATA_DIR / "patterns.json"
TOP_K = 20
PARSER_VERSION = 3


# ── salaries ─────────────────────────────────────────────────────────────────
def fetch_salaries(day: date) -> pd.DataFrame:
    path = CACHE / f"{day.isoformat()}.csv"
    if path.exists():
        df = pd.read_csv(path)
        if not df.empty:
            return df
        path.unlink()                      # an empty cache is a failed parse: fetch again
    r = session().get(ROTOGURU.format(m=day.month, d=day.day, y=day.year), timeout=30)
    r.raise_for_status()
    df = parse_rotoguru(r.text)
    if df.empty:
        # keep one raw page so the parser can be checked against what the site really sends
        dbg = DATA_DIR / "debug" / "rotoguru_sample.html"
        dbg.parent.mkdir(parents=True, exist_ok=True)
        if not dbg.exists():
            dbg.write_text(f"<!-- {r.url} status {r.status_code} -->\n" + r.text[:400_000])
        return df
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


VERSION = 2
SIZE_BUCKETS = (("small", 1, 3), ("medium", 4, 6), ("large", 7, 99))
RANK_BUCKETS = ((1, 3), (4, 6), (7, 10), (11, 15), (16, 25), (26, 40), (41, 999))


def size_bucket(n_games: int) -> str:
    for name, lo, hi in SIZE_BUCKETS:
        if lo <= n_games <= hi:
            return name
    return "large"


def rank_bucket(rank: float) -> str:
    for lo, hi in RANK_BUCKETS:
        if lo <= rank <= hi:
            return f"{lo}-{hi}" if hi < 999 else f"{lo}+"
    return "41+"


def sub_slates(df: pd.DataFrame) -> list[tuple[str, pd.DataFrame]]:
    """The night as DraftKings would slice it: all games, the main slate (7pm ET+)
    and a late slate (9:30pm ET+), when those differ and have 2+ games."""
    out = [("all", df)]
    if "start_utc" not in df.columns or df["start_utc"].astype(str).str.len().eq(0).all():
        return out
    st = pd.to_datetime(df["start_utc"], utc=True, errors="coerce").dt.tz_convert("America/New_York")
    mins = st.dt.hour * 60 + st.dt.minute
    mins = mins.where(st.dt.hour >= 6, mins + 24 * 60)     # after-midnight tips belong to the night
    n_all = df["game_id"].nunique()
    for name, cutoff in (("main", 19 * 60), ("late", 21 * 60 + 30)):
        sub = df[mins >= cutoff]
        g = sub["game_id"].nunique()
        if 2 <= g < n_all and all(o[1]["game_id"].nunique() != g for o in out):
            out.append((name, sub))
    return out


def _lineup_record(sub: pd.DataFrame) -> dict:
    team_counts = sub["team"].value_counts()
    game_key = sub.apply(lambda r: "|".join(sorted([r["team"], r["opp"]])), axis=1)
    game_counts = game_key.value_counts()
    both_sides = any(sub.loc[game_key == g, "team"].nunique() == 2 for g in game_counts.index)
    return {
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
    }


def analyse_night(day: date, joined: pd.DataFrame, ctx: pd.DataFrame) -> dict | None:
    cols = ["person_id", "game_id", "teammate_out", "surprise_start", "minutes_spike", "overtime",
            "blowout", "high_total", "beat_proj", "pred"]
    df = joined.merge(ctx[cols], on=["person_id", "game_id"], how="left").reset_index(drop=True)
    df = df[df["dk_fp"] > 0].reset_index(drop=True)
    slates = []
    for kind, part in sub_slates(df):
        part = part.reset_index(drop=True).copy()
        lus = top_lineups(part)
        if not lus:
            continue
        # what was knowable before the games: projection, value and their ranks
        part["proj_value"] = part["pred"].fillna(0) / (part["salary"] / 1000)
        part["proj_value_rank"] = part["proj_value"].rank(ascending=False, method="first")
        part["proj_rank"] = part["pred"].fillna(0).rank(ascending=False, method="first")
        counts = np.zeros(len(part))
        for lu in lus:
            counts[lu] += 1
        players = [{
            "salary": int(r.salary), "value_rank": int(r.proj_value_rank), "proj_rank": int(r.proj_rank),
            "teammate_out": bool(r.teammate_out) if not pd.isna(r.teammate_out) else False,
            "starter": bool(r.starter), "exp": round(float(c / len(lus)), 3),
        } for r, c in zip(part.itertuples(index=False), counts)]
        slates.append({"type": kind, "games": int(part["game_id"].nunique()),
                       "lineups": [_lineup_record(part.iloc[lu]) for lu in lus], "players": players})
    if not slates:
        return None
    return {"version": VERSION, "date": day.isoformat(), "slates": slates}


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
def _structure(lus: list[dict]) -> dict:
    def pct(x):
        return round(100.0 * float(np.mean(x)), 1)
    sal = np.array([lu["salary"] for lu in lus])
    max_team = np.array([lu["max_team"] for lu in lus])
    max_game = np.array([lu["max_game"] for lu in lus])
    players = [p for lu in lus for p in lu["players"]]
    beat = [p["beat_proj"] for p in players if p["beat_proj"]]
    rank = [p["proj_value_rank"] for p in players if p["proj_value_rank"]]
    return {
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
        "rules": {
            "min_salary": int(max(45000, (np.percentile(sal, 5) // 100) * 100)),
            "max_per_team": int(max(3, np.percentile(max_team, 99))),
            "max_per_game": int(max(4, np.percentile(max_game, 99))),
        },
    }


def exposure_model(slates: list[dict], shrink: float = 30.0) -> dict:
    """Average share of the top lineups that included players of each type: by
    slate size, pre-game value rank and whether a rotation teammate was out.
    This is the data-driven target exposure for a lineup portfolio."""
    out: dict = {}
    for name, _, _ in SIZE_BUCKETS:
        rows = [p for s in slates if size_bucket(s["games"]) == name for p in s["players"]]
        if not rows:
            continue
        base = float(np.mean([p["exp"] for p in rows]))
        table = {}
        for lo, hi in RANK_BUCKETS:
            rb = rank_bucket(lo)
            for tm in (False, True):
                cell = [p["exp"] for p in rows if lo <= p["value_rank"] <= hi and p["teammate_out"] == tm]
                n = len(cell)
                raw = float(np.mean(cell)) if n else base
                table[f"{rb}|{int(tm)}"] = {"exp": round((n * raw + shrink * base) / (n + shrink) * 100, 2), "n": n}
        out[name] = {"table": table, "players": len(rows),
                     "slates": sum(1 for s in slates if size_bucket(s["games"]) == name)}
    return out


def suggested_exposure(model_: dict, n_games: int, value_rank: int, teammate_out: bool) -> float | None:
    m = model_.get(size_bucket(n_games)) or next(iter(model_.values()), None)
    if not m:
        return None
    cell = m["table"].get(f"{rank_bucket(value_rank)}|{int(bool(teammate_out))}")
    return cell["exp"] if cell else None


def summarise(nights: list[dict], contests: list[dict]) -> dict:
    slates = [dict(s, date=n["date"]) for n in nights for s in n.get("slates", [])]
    if not slates:
        return {"nights": 0, "rules": default_rules(), "buckets": {},
                "note": "No verified historical nights yet; using default rules."}
    buckets = {}
    for name, lo, hi in SIZE_BUCKETS:
        mine = [s for s in slates if size_bucket(s["games"]) == name]
        lus = [lu for s in mine for lu in s["lineups"]]
        if not lus:
            continue
        buckets[name] = {"label": f"{lo}-{hi if hi < 99 else '15'} games", "slates": len(mine),
                         "lineups_analysed": len(lus), **_structure(lus)}
    all_lus = [lu for s in slates if s["type"] == "all" for lu in s["lineups"]] or \
              [lu for s in slates for lu in s["lineups"]]
    overall = _structure(all_lus)
    return {
        "version": VERSION,
        "nights": len(nights),
        "slates_analysed": len(slates),
        "lineups_analysed": sum(len(s["lineups"]) for s in slates),
        "date_range": [nights[0]["date"], nights[-1]["date"]],
        **overall,
        "buckets": buckets,
        "exposure_model": exposure_model(slates),
        "real_contests": {"count": len(contests), "contests": contests[-20:]},
    }


def default_rules() -> dict:
    return {"min_salary": 48500, "max_per_team": 4, "max_per_game": 6}


def _load() -> dict:
    try:
        return json.loads(OUT.read_text())
    except (OSError, ValueError):
        return {}


def load_rules(n_games: int | None = None) -> dict:
    doc = _load()
    if n_games is not None:
        b = (doc.get("buckets") or {}).get(size_bucket(n_games))
        if b and b.get("rules"):
            return b["rules"]
    return doc.get("rules") or default_rules()


def load_exposure_model() -> dict:
    return _load().get("exposure_model") or {}


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
    done = set()
    for p in NIGHTS.glob("*.json"):
        try:
            rec = json.loads(p.read_text())
        except ValueError:
            continue
        # verified nights are redone only when the analysis changes; unverified ones
        # again when the salary parser changes (a parser bug must not stick forever)
        if (rec.get("verified") and rec.get("version") == VERSION) or \
                (not rec.get("verified") and rec.get("parser") == PARSER_VERSION):
            done.add(p.stem)
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
        rec = {"date": day.isoformat(), "verified": rate >= 0.95, "agreement": round(rate, 3),
               "version": VERSION, "parser": PARSER_VERSION, "salary_rows": int(len(sal))}
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
        if n.get("verified") and n.get("slates"):
            nights.append(n)
    contests = []   # your own contest results are private: see pipeline/contests.py
    summary = summarise(nights, contests)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(summary, indent=1))
    log.info("patterns: %d verified nights, rules %s", summary.get("nights", 0), summary.get("rules"))
    return summary


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    run(int(sys.argv[1]) if len(sys.argv) > 1 else 400)
