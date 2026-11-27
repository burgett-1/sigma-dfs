"""NFL DraftKings scoring (registry table nfl-v1) and the nflverse column mapping."""
from pathlib import Path

import pandas as pd
import pytest

from pipeline.sports import scoring
from pipeline.sports.nfl import data as D

FIX = Path(__file__).parent / "fixtures" / "nfl"
OFF = scoring.table_for("NFL", "WR")
DST = scoring.table_for("NFL", "DST")
K = scoring.table_for("NFL", "K")

# (stat line, expected DK points), all computed by hand from the DK rules
OFFENSE_CASES = [
    ({"pass_yds": 299}, 11.96),
    ({"pass_yds": 300}, 15.0),                        # 12 + 3 bonus
    ({"pass_yds": 301}, 15.04),
    ({"pass_yds": 250, "pass_td": 2, "int": 1}, 17.0),
    ({"pass_yds": 320, "pass_td": 3, "int": 2, "rush_yds": 25}, 12.8 + 3 + 12 - 2 + 2.5),
    ({"rush_yds": 99}, 9.9),
    ({"rush_yds": 100}, 13.0),
    ({"rec_yds": 99, "rec": 6}, 15.9),
    ({"rec_yds": 100, "rec": 6}, 19.0),
    ({"rush_yds": 100, "rec_yds": 100, "rec": 3}, 10 + 3 + 10 + 3 + 3),      # +6 for two bonuses
    ({"rush_yds": 120, "rush_td": 2, "rec": 4, "rec_yds": 30}, 12 + 3 + 12 + 4 + 3),
    ({"rec": 10, "rec_yds": 150, "rec_td": 2}, 10 + 15 + 3 + 12),
    ({"two_pt": 1}, 2.0),                            # 2-pt pass
    ({"two_pt": 2}, 4.0),                            # 2-pt rush + rec
    ({"fum_lost": 1}, -1.0),
    ({"int": 1}, -1.0),
    ({"ret_td": 1}, 6.0),                            # kick / punt return TD
    ({"fum_rec_td": 1}, 6.0),                        # offensive fumble-recovery TD
    ({"pass_yds": 0, "rush_yds": -5}, -0.5),
    ({}, 0.0),
    ({"pass_yds": 400, "pass_td": 4, "rush_yds": 100, "rush_td": 1}, 16 + 3 + 16 + 10 + 3 + 6),
    ({"rec": 1, "rec_yds": 75, "rec_td": 1, "fum_lost": 1}, 1 + 7.5 + 6 - 1),
]
# points allowed buckets: 0 -> 10, 1-6 -> 7, 7-13 -> 4, 14-20 -> 1, 21-27 -> 0, 28-34 -> -1, 35+ -> -4
PA_CASES = [(0, 10), (1, 7), (6, 7), (7, 4), (13, 4), (14, 1), (20, 1), (21, 0), (27, 0), (28, -1), (34, -1),
            (35, -4), (52, -4)]
DST_CASES = [
    ({"sack": 3, "points_allowed": 17}, 4.0),
    ({"int": 2, "fum_rec": 1, "points_allowed": 24}, 6.0),
    ({"safety": 1, "blocked_kick": 1, "points_allowed": 10}, 8.0),
    ({"def_st_td": 1, "points_allowed": 0}, 16.0),
    ({"xp_return": 1, "points_allowed": 30}, 1.0),
    ({"sack": 5, "int": 1, "fum_rec": 2, "def_st_td": 2, "safety": 1, "blocked_kick": 1, "points_allowed": 3},
     5 + 2 + 4 + 12 + 2 + 2 + 7),
]
K_CASES = [({"xp": 3, "fg_0_39": 1, "fg_40_49": 1, "fg_50p": 1}, 3 + 3 + 4 + 5), ({"xp": 0}, 0.0)]


@pytest.mark.parametrize("line,pts", OFFENSE_CASES)
def test_offense_scoring(line, pts):
    assert scoring.score(line, OFF) == pytest.approx(pts, abs=0.01)


@pytest.mark.parametrize("pa,pts", PA_CASES)
def test_dst_points_allowed_boundaries(pa, pts):
    assert scoring.score({"points_allowed": pa}, DST) == pytest.approx(pts, abs=0.01)


@pytest.mark.parametrize("line,pts", DST_CASES)
def test_dst_categories(line, pts):
    assert scoring.score(line, DST) == pytest.approx(pts, abs=0.01)


@pytest.mark.parametrize("line,pts", K_CASES)
def test_kicker(line, pts):
    assert scoring.score(line, K) == pytest.approx(pts, abs=0.01)


def test_frame_scoring_matches_dict_scoring():
    df = pd.DataFrame([line for line, _ in OFFENSE_CASES]).fillna(0)
    assert list(scoring.score(df, OFF)) == pytest.approx([p for _, p in OFFENSE_CASES], abs=0.01)


def test_scoring_unverified_flag():
    from pipeline import sports
    assert sports.get("NFL")["scoring"]["verified"] is False


def test_nflverse_column_mapping_on_real_format_rows():
    pw = pd.read_csv(FIX / "nflverse_player_week_sample.csv")
    tw = pd.read_csv(FIX / "nflverse_team_week_sample.csv")
    gm = pd.read_csv(FIX / "nflverse_games_sample.csv")
    h = D.build_history(pw, tw, gm)
    assert set(h["position"]) <= {"QB", "RB", "WR", "TE", "K", "DST"}
    assert (h["position"] == "DST").sum() == len(tw)
    # every offensive row: DK points recomputed by hand from the raw nflverse columns
    raw = pw.set_index("player_id")
    for _, r in h[h["position"].isin(["QB", "RB", "WR", "TE"])].iterrows():
        x = raw.loc[r["player_id"]]
        x = x.iloc[0] if isinstance(x, pd.DataFrame) else x
        f = lambda c: float(x.get(c, 0) or 0)   # noqa: E731
        pts = (0.04 * f("passing_yards") + 4 * f("passing_tds") - f("passing_interceptions")
               + 0.1 * f("rushing_yards") + 6 * f("rushing_tds") + f("receptions") + 0.1 * f("receiving_yards")
               + 6 * f("receiving_tds") - (f("rushing_fumbles_lost") + f("receiving_fumbles_lost") + f("sack_fumbles_lost"))
               + 2 * (f("passing_2pt_conversions") + f("rushing_2pt_conversions") + f("receiving_2pt_conversions"))
               + 6 * f("special_teams_tds") + 6 * f("fumble_recovery_tds")
               + 3 * (f("passing_yards") >= 300) + 3 * (f("rushing_yards") >= 100) + 3 * (f("receiving_yards") >= 100))
        assert r["dk_fp"] == pytest.approx(pts, abs=0.01), r["name"]
    # DST points allowed = the opponent's final score from games.csv
    dst = h[h["position"] == "DST"].iloc[0]
    g = gm[(gm["home_team"] == dst["team"]) | (gm["away_team"] == dst["team"])].iloc[0]
    opp_score = g["away_score"] if g["home_team"] == dst["team"] else g["home_score"]
    assert dst["points_allowed"] == opp_score


def test_return_fumble_counts_via_fumbles_lost_total():
    """F4: a fumble lost on a return/lateral shows only in fumbles_lost_total."""
    pw = pd.read_csv(FIX / "nflverse_player_week_sample.csv")
    tw = pd.read_csv(FIX / "nflverse_team_week_sample.csv")
    gm = pd.read_csv(FIX / "nflverse_games_sample.csv")
    row = pw[pw["position"] == "WR"].iloc[[0]].copy()
    for c in row.columns:
        if pd.api.types.is_numeric_dtype(row[c]) and c not in ("season", "week"):
            row[c] = 0
    row["player_id"] = "00-RETURNER"
    row["fumbles_lost_total"] = 1
    h = D.build_history(pd.concat([pw, row], ignore_index=True), tw, gm)
    r = h[h["player_id"] == "00-RETURNER"].iloc[0]
    assert r["fum_lost"] == 1 and r["dk_fp"] == pytest.approx(-1.0)
    # without the total column the three-part sum is used, as before
    legacy = D.build_history(pw.drop(columns=["fumbles_lost_total"]), tw, gm)
    assert len(legacy) == len(D.build_history(pw, tw, gm))


def test_poisoned_cache_file_on_disk_is_replaced():
    """F1: a bad file cached before validation existed is discarded and refetched,
    even for a past season (cached forever)."""
    from pipeline.sports.paths import paths
    cache = paths("NFL").cache
    cache.mkdir(parents=True, exist_ok=True)
    (cache / "stats_player_week_2025.csv").write_text("<html>503 Service Unavailable</html>")
    good = {"stats_player": (FIX / "nflverse_player_week_sample.csv").read_text(),
            "stats_team": (FIX / "nflverse_team_week_sample.csv").read_text(),
            "games": (FIX / "nflverse_games_sample.csv").read_text()}
    calls = []

    def get(url):
        calls.append(url)
        return next(v for k, v in good.items() if k in url)
    df = D.player_week(2025, None, get)
    assert "player_id" in df and len(df) > 0 and any("stats_player_week_2025" in c for c in calls)
    assert D.PLAYER_WEEK_OK((cache / "stats_player_week_2025.csv").read_text())
    # an invalid response is never cached: with no valid copy it raises, with one it is ignored
    with pytest.raises(ValueError):
        D.team_week(2024, None, lambda url: "<html>oops</html>")
    assert not (cache / "stats_team_week_2024.csv").exists()
    assert len(D.player_week(2025, 0.0, lambda url: "nonsense")) == len(df)


def _real_contests():
    import os
    d = Path(os.environ.get("SIGMA_TEST_REAL_CONTESTS", "/nonexistent"))
    return sorted(d.glob("contest-standings-*.csv")) if d.exists() else []


NFLVERSE_DIR = Path(__import__("os").environ.get("SIGMA_TEST_NFLVERSE_DIR", "/nonexistent"))


@pytest.mark.skipif(not _real_contests() or not (NFLVERSE_DIR / "stats_player_week_2026.csv").exists(),
                    reason="needs the owner's contest exports and SIGMA_TEST_NFLVERSE_DIR (read-only, aggregates only)")
def test_real_fpts_against_owner_nfl_exports():
    """AC24 as restated (P3-5 Wave 1): >= 90% of rostered names with FPTS != 0 join
    the nflverse week; >= 97% of joined QB/RB/WR/TE within 0.01; DST >= 90% within 1.0.
    Reads private exports read-only and asserts on aggregates only."""
    from pipeline import contests
    from pipeline.names import norm_name
    from pipeline.sports.nfl import teams as T
    yrs = [2026]
    pw = pd.concat([pd.read_csv(NFLVERSE_DIR / f"stats_player_week_{y}.csv", low_memory=False) for y in yrs])
    tw = pd.concat([pd.read_csv(NFLVERSE_DIR / f"stats_team_week_{y}.csv", low_memory=False) for y in yrs])
    hist = D.build_history(pw, tw, pd.read_csv(NFLVERSE_DIR / "games.csv"))
    nick = {norm_name(t["nickname"]) for t in T.TEAMS}
    nonzero = joined = off_n = off_ok = dst_n = dst_ok = 0
    seen = set()
    for f in _real_contests():
        c = contests.parse(f.read_text(encoding="utf-8-sig", errors="replace")) if f.stat().st_size else None
        if not c or c["sport"] != "NFL":
            continue
        key = tuple(sorted(c["fpts"].items())[:5])
        if key in seen:
            continue
        seen.add(key)
        best = None                                         # the week whose players match best
        for wk, cur in hist.groupby("week"):
            m = {norm_name(str(n)): (p, v) for n, p, v in zip(cur["name"], cur["position"], cur["dk_fp"])}
            hit = sum(1 for k in c["fpts"] if k in m)
            if best is None or hit > best[0]:
                best = (hit, m)
        m = best[1]
        for k, v in c["fpts"].items():
            if abs(v) >= 0.01:
                nonzero += 1
                joined += k in m
            if k not in m:
                continue
            pos, fp = m[k]
            if pos == "DST" or k in nick:
                dst_n += 1
                dst_ok += abs(fp - v) <= 1.0
            elif pos != "K":
                off_n += 1
                off_ok += abs(fp - v) <= 0.01
    if not seen:
        pytest.skip("no NFL exports in the folder")
    assert joined / nonzero >= 0.90 and off_ok / off_n >= 0.97 and (not dst_n or dst_ok / dst_n >= 0.90)


def test_truncated_past_season_file_is_rejected_and_refetched():
    """R1-B2: a download cut short mid-file (valid header, partial last row or no
    trailing newline) is never cached, and a truncated file already cached for a
    past season (kept forever) is discarded and refetched."""
    from pipeline.sports.paths import paths
    full = (FIX / "nflverse_player_week_sample.csv").read_text()
    for cut in (full[: len(full) // 3], full[: len(full) // 2], full.rstrip("\n")):
        assert not D.PLAYER_WEEK_OK(cut)
    assert D.PLAYER_WEEK_OK(full)
    cache = paths("NFL").cache
    cache.mkdir(parents=True, exist_ok=True)
    with pytest.raises(ValueError):
        D.player_week(2023, None, lambda url: full[: len(full) // 3])
    assert not (cache / "stats_player_week_2023.csv").exists()
    (cache / "stats_player_week_2023.csv").write_text(full[: len(full) // 2])     # cached before the check
    df = D.player_week(2023, None, lambda url: full)
    assert len(df) == len(pd.read_csv(FIX / "nflverse_player_week_sample.csv"))
    assert (cache / "stats_player_week_2023.csv").read_text() == full
