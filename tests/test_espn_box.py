from datetime import date, datetime, timezone

import pytest

from pipeline import history
from pipeline.sources import espn_box

LABELS = ["MIN", "PTS", "FG", "3PT", "FT", "REB", "AST", "TO", "STL", "BLK", "OREB", "DREB", "PF", "+/-"]


def _ath(i, name, starter, stats=None, dnp=False, reason=""):
    return {"athlete": {"id": str(i), "displayName": name, "position": {"abbreviation": "G"}},
            "starter": starter, "didNotPlay": dnp, "reason": reason, "stats": stats or []}


SUMMARY = {
    "header": {"id": "401700001", "competitions": [{
        "date": "2026-11-16T00:30Z",
        "status": {"period": 3, "displayClock": "6:00", "type": {"state": "in", "completed": False}},
        "competitors": [{"homeAway": "home", "score": "88", "team": {"abbreviation": "LAL"}},
                        {"homeAway": "away", "score": "80", "team": {"abbreviation": "GS"}}]}]},
    "gameInfo": {"officials": [{"displayName": "Scott Foster"}, {"displayName": "Tony Brothers"}]},
    "boxscore": {"players": [
        {"team": {"abbreviation": "LAL"}, "statistics": [{"labels": LABELS, "athletes": [
            _ath(1, "LeBron James", True, ["34", "30", "11-20", "2-5", "6-8", "10", "9", "4", "2", "1", "1", "9", "2", "+8"]),
            _ath(2, "Bench Guy", False, dnp=True, reason="COACH'S DECISION"),
            _ath(3, "Hurt Guy", False, dnp=True, reason="Left Ankle Sprain"),
        ]}]},
        {"team": {"abbreviation": "GS"}, "statistics": [{"labels": LABELS, "athletes": [
            _ath(4, "Stephen Curry", True, ["36", "28", "9-19", "6-12", "4-4", "5", "7", "3", "1", "0", "0", "5", "1", "-8"]),
        ]}]},
    ]},
}


def test_summary_rows_and_dk_points():
    rows = espn_box.parse_summary(SUMMARY, "401700001")
    by = {r["name"]: r for r in rows}
    lbj = by["LeBron James"]
    assert lbj["team"] == "LAL" and lbj["opp"] == "GSW" and lbj["home"] and lbj["starter"]
    assert (lbj["minutes"], lbj["pts"], lbj["reb"], lbj["ast"], lbj["stl"], lbj["blk"], lbj["tov"]) == (34, 30, 10, 9, 2, 1, 4)
    assert (lbj["fg3m"], lbj["fga"], lbj["fta"]) == (2, 20, 8)
    assert lbj["team_score"] == 88 and lbj["opp_score"] == 80 and lbj["date"] == "2026-11-15"
    assert lbj["officials"] == "Scott Foster|Tony Brothers" and lbj["game_id"] == "E401700001"
    assert by["Bench Guy"]["not_playing_reason"] == "DNP_COACH" and by["Bench Guy"]["minutes"] == 0
    assert by["Hurt Guy"]["not_playing_reason"] == "INACTIVE_INJURY" and by["Hurt Guy"]["status"] == "INACTIVE"
    import pandas as pd
    df = pd.DataFrame(rows)
    fp = dict(zip(df["name"], history.dk_points(df)))
    # 30 + 2*0.5 + 10*1.25 + 9*1.5 + 2*2 + 1*2 - 4*0.5 = 61.0 (+1.5 double-double) = 62.5
    assert fp["LeBron James"] == 62.5


def test_progress():
    assert espn_box.progress(SUMMARY) == (pytest.approx(30 / 48), "in")
    done = {"header": {"competitions": [{"status": {"type": {"state": "post"}}}]}}
    assert espn_box.progress(done) == (1.0, "post")
    late = {"header": {"competitions": [{"status": {"period": 4, "displayClock": "45.2", "type": {"state": "in"}}}]}}
    assert espn_box.progress(late)[0] > 0.98


def test_scoreboard_and_sync(tmp_path, monkeypatch):
    sb = {"events": [
        {"id": "1", "date": "2026-11-16T00:30Z", "season": {"type": 2},
         "status": {"type": {"state": "post", "completed": True}},
         "competitions": [{"competitors": [{"homeAway": "home", "team": {"abbreviation": "LAL"}},
                                           {"homeAway": "away", "team": {"abbreviation": "GS"}}]}]},
        {"id": "2", "date": "2026-11-16T00:30Z", "season": {"type": 1},   # preseason: ignored
         "status": {"type": {"state": "post", "completed": True}}, "competitions": [{"competitors": []}]},
    ]}
    games = espn_box.parse_scoreboard(sb)
    assert [g["game_id"] for g in games] == ["E1"] and games[0]["home"] == "LAL" and games[0]["away"] == "GSW"
    monkeypatch.setattr(history, "HISTORY_DIR", tmp_path)
    calls = {"sb": 0, "sum": 0}

    def fake_games(d):
        calls["sb"] += 1
        return games if d == date(2026, 11, 15) else []

    def fake_summary(eid):
        calls["sum"] += 1
        return SUMMARY
    monkeypatch.setattr(espn_box, "games_on", fake_games)
    monkeypatch.setattr(espn_box, "summary", fake_summary)
    now = datetime(2026, 11, 20, 18, tzinfo=timezone.utc)
    df = history.update_current_season(history.load(), now)
    assert df["game_id"].nunique() == 1 and len(df) == 4
    history.save(df)
    n_sb = calls["sb"]
    df2 = history.update_current_season(history.load(), now)       # second pass: only recent dates rechecked
    assert calls["sum"] == 1 and calls["sb"] - n_sb <= 3 and len(df2) == 4
