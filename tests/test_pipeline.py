import csv
import json
from datetime import date, timedelta

import pytest

from pipeline import injuries, model, notify, ownership
from pipeline.names import norm_name, player_key, team_code
from pipeline.sources import draftkings, espn, injury_report, nba_cdn
from tests import synthetic as syn

TODAY = date(2026, 11, 15)
TIP = syn.utc(2026, 11, 16, 0, 30)   # 7:30pm ET


# ── names ────────────────────────────────────────────────────────────────────
def test_names_and_teams():
    assert norm_name("Jaren Jackson Jr.") == "jaren jackson"
    assert norm_name("Nikola Jokić") == "nikola jokic"
    assert norm_name("De'Aaron Fox") == "deaaron fox"
    assert team_code("GS") == "GSW" and team_code("NO") == "NOP" and team_code("UTAH") == "UTA"
    assert team_code("Los Angeles Lakers") == "LAL" and team_code("LA Clippers") == "LAC"
    assert player_key("Jaren Jackson Jr.", "MEM") == player_key("Jaren Jackson", "MEM")


# ── sources ──────────────────────────────────────────────────────────────────
def test_draftkings_parsing(monkeypatch):
    now = syn.utc(2026, 11, 15, 18)
    monkeypatch.setattr(draftkings, "get_json", lambda url: syn.lobby(TIP) if "lobby" in url else
                        syn.showdown_draftables(TIP) if "556" in url else syn.draftables(TIP))
    slates = draftkings.list_draft_groups(now)
    assert [s.draft_group_id for s in slates] == [555, 556]
    sd = draftkings.load_draftables(slates[1])
    assert sd.kind == "showdown" and len(sd.players) == 20 and len(sd.games) == 1
    p0 = sd.players[0]
    assert p0["cpt_id"] == p0["draftable_id"] + 500000 and p0["cpt_salary"] == int(p0["salary"] * 1.5)
    s = draftkings.load_draftables(slates[0])
    assert s.kind == "classic"
    assert len(s.players) == 40
    p = next(p for p in s.players if p["team_dk"] == "GS")
    assert p["team"] == "GSW" and p["opp"] == "NYK" and p["opp_dk"] == "NY" and not p["home"]
    assert p["start_utc"] == TIP + timedelta(hours=2)


def test_dk_date_formats():
    assert draftkings.parse_dt("2026-11-16T00:30:00.0000000Z") == TIP
    assert draftkings.parse_dt("/Date(1763253000000)/").year == 2025


def test_espn_odds():
    data = {"events": [{"competitions": [{
        "competitors": [{"homeAway": "home", "team": {"abbreviation": "LAL"}},
                        {"homeAway": "away", "team": {"abbreviation": "BOS"}}],
        "odds": [{"details": "BOS -6.5", "overUnder": 226.5}]}]}]}
    o = espn.parse_scoreboard_odds(data)
    assert o["BOS"]["spread"] == -6.5 and o["LAL"]["spread"] == 6.5
    assert o["BOS"]["implied"] == pytest.approx(116.5)
    assert o["LAL"]["implied"] == pytest.approx(110.0)


def test_espn_injuries():
    data = {"injuries": [{"displayName": "Boston Celtics", "injuries": [
        {"athlete": {"displayName": "Jayson Tatum"}, "status": "Out", "shortComment": "Achilles"},
        {"athlete": {"displayName": "Derrick White"}, "status": "Day-To-Day"}]}]}
    rows = espn.parse_injuries(data)
    assert rows[0]["team"] == "BOS" and rows[0]["status"] == "OUT"
    assert rows[1]["status"] == "QUESTIONABLE"


def test_official_report_lines():
    lines = [
        "Injury Report: 11/15/26 05:15 PM",
        "Game Date Game Time Matchup Team Player Name Current Status Reason",
        "11/15/2026 07:30 (ET) BOS@LAL Boston Celtics Tatum, Jayson Out Injury/Illness - Right Achilles;",
        "Repair",
        "White, Derrick Questionable Injury/Illness - Left Knee; Soreness",
        "Los Angeles Lakers James, LeBron Probable Injury/Illness - Left Foot; Arthritis",
        "Jackson Jr., Jaren Doubtful Rest",
        "09:30 (ET) GSW@NYK Golden State Warriors NOT YET SUBMITTED",
        "Page 1 of 2",
    ]
    rows = injury_report.parse_lines(lines)
    players = [r for r in rows if r.get("name")]
    assert [(r["name"], r["team"], r["status"]) for r in players] == [
        ("Jayson Tatum", "BOS", "OUT"), ("Derrick White", "BOS", "QUESTIONABLE"),
        ("LeBron James", "LAL", "PROBABLE"), ("Jaren Jackson Jr.", "LAL", "DOUBTFUL")]
    assert players[0]["reason"].endswith("Repair")
    assert players[0]["game_date"] == "11/15/2026"
    assert any(r.get("not_submitted") and r["team"] == "GSW" for r in rows)


def test_report_url_candidates():
    c = injury_report.candidate_urls(syn.utc(2026, 11, 15, 22, 20), hours_back=1)
    urls = [u for _, u in c]
    assert urls[0].endswith("Injury-Report_2026-11-15_05_15PM.pdf")
    assert any(u.endswith("Injury-Report_2026-11-15_05PM.pdf") for u in urls)
    assert injury_report._url_time(urls[0]).hour == 17


def test_minutes_parsing():
    assert nba_cdn.parse_minutes("PT34M12.00S") == pytest.approx(34.2)
    assert nba_cdn.parse_minutes("31:30") == pytest.approx(31.5)
    assert nba_cdn.parse_minutes(None) == 0.0


def test_dk_scoring():
    import pandas as pd
    from pipeline.history import dk_points
    df = pd.DataFrame([{"pts": 30, "reb": 12, "ast": 10, "stl": 1, "blk": 1, "tov": 4, "fg3m": 3}])
    # 30 + 1.5 + 15 + 15 + 2 + 2 - 2 = 63.5, + DD 1.5 + TD 3 = 68
    assert dk_points(df).iloc[0] == 68.0


# ── model ────────────────────────────────────────────────────────────────────
def _slate_players(monkeypatch):
    monkeypatch.setattr(draftkings, "get_json", lambda url: syn.draftables(TIP))
    s = draftkings.load_draftables(draftkings.Slate(555, "Main", TIP, 2, True))
    for p in s.players:
        p["key"] = player_key(p["name"], p["team"])
    return s.players


def test_projection_basics(monkeypatch):
    players = _slate_players(monkeypatch)
    logs = syn.season_logs(TODAY)
    rows = model.project(players, logs, model.Context(today=TODAY))
    by = {r["name"]: r for r in rows}
    star = by[syn.name("BOS", 0)]
    assert 34 < star["proj"] < 52  # ~34 min x ~1.25 fp/min
    assert star["starting"] and not by[syn.name("BOS", 9)]["starting"]
    for t in ("BOS", "LAL"):
        tot = sum(r["proj_minutes"] for r in rows if r["team"] == t)
        assert tot == pytest.approx(241, abs=0.5)
    assert all(r["ceiling"] > r["proj"] > r["floor"] >= 0 for r in rows if r["proj"] > 0)


def test_injury_redistribution(monkeypatch):
    players = _slate_players(monkeypatch)
    logs = syn.season_logs(TODAY)
    base = {r["name"]: r for r in model.project(players, logs, model.Context(today=TODAY))}
    star = next(p for p in players if p["name"] == syn.name("BOS", 0))
    ctx = model.Context(today=TODAY, statuses={star["key"]: {"status": "OUT"}})
    after = {r["name"]: r for r in model.project(players, logs, ctx)}
    assert after[syn.name("BOS", 0)]["proj"] == 0
    tot = sum(r["proj_minutes"] for r in after.values() if r["team"] == "BOS")
    assert tot == pytest.approx(241, abs=0.5)
    # same-position backup (PG/SG) gains the most minutes; everyone gains points
    gains = {n: after[n]["proj_minutes"] - base[n]["proj_minutes"] for n in after if n.startswith("Bos Player") and n != syn.name("BOS", 0)}
    assert max(gains, key=gains.get) == syn.name("BOS", 5)
    assert all(after[n]["proj"] > base[n]["proj"] for n in gains)
    # other teams unaffected
    assert after[syn.name("GSW", 0)]["proj"] == pytest.approx(base[syn.name("GSW", 0)]["proj"])


def test_vegas_and_blowout(monkeypatch):
    players = _slate_players(monkeypatch)
    logs = syn.season_logs(TODAY)
    base = {r["name"]: r for r in model.project(players, logs, model.Context(today=TODAY))}
    odds = {"BOS": {"implied": 124.0, "spread": -16.0}, "LAL": {"implied": 108.0, "spread": 16.0}}
    after = {r["name"]: r for r in model.project(players, logs, model.Context(today=TODAY, odds=odds))}
    assert after[syn.name("BOS", 0)]["proj_minutes"] < base[syn.name("BOS", 0)]["proj_minutes"]
    assert after[syn.name("BOS", 8)]["proj_minutes"] > base[syn.name("BOS", 8)]["proj_minutes"]
    assert after[syn.name("LAL", 2)]["proj"] < base[syn.name("LAL", 2)]["proj"]


def test_rookie_without_history(monkeypatch):
    players = _slate_players(monkeypatch)
    players.append({**players[0], "dk_player_id": 1, "draftable_id": 1, "name": "New Rookie",
                    "salary": 5000, "key": player_key("New Rookie", "BOS")})
    rows = {r["name"]: r for r in model.project(players, syn.season_logs(TODAY), model.Context(today=TODAY))}
    assert 10 < rows["New Rookie"]["proj"] < 30


def test_ownership_and_opt(monkeypatch):
    players = _slate_players(monkeypatch)
    rows = model.project(players, syn.season_logs(TODAY), model.Context(today=TODAY))
    ownership.estimate_ownership(rows)
    assert sum(r["own"] for r in rows) == pytest.approx(800, rel=1e-3)
    ownership.optimal_rates(rows, sims=150)
    assert sum(r["opt_pct"] for r in rows) == pytest.approx(800, rel=1e-3)


def test_assignable():
    ok = [{"PG"}, {"SG"}, {"SF"}, {"PF"}, {"C"}, {"PG", "SG"}, {"SF"}, {"C"}]
    bad = [{"C"}] * 8
    assert ownership._assignable(ok) and not ownership._assignable(bad)


# ── injury merge / diff / notify ─────────────────────────────────────────────
def test_merge_priority():
    players = [{"key": player_key("A B", "BOS"), "name": "A B", "team": "BOS", "dk_status": "QUESTIONABLE"},
               {"key": player_key("C D", "BOS"), "name": "C D", "team": "BOS", "dk_status": "OUT"},
               {"key": player_key("E F", "NYK"), "name": "E F", "team": "NYK", "dk_status": ""}]
    src = {"nba_rows": [{"name": "A B", "team": "BOS", "status": "OUT", "game_date": "11/15/2026"}],
           "espn_rows": [{"name": "E F", "team": "NYK", "status": "QUESTIONABLE"},
                         {"name": "C D", "team": "BOS", "status": "OUT"}]}
    m = injuries.merge(players, src, "2026-11-15")
    assert m[players[0]["key"]]["status"] == "OUT"      # official wins
    assert m[players[1]["key"]]["status"] == ""         # BOS submitted and C D is not listed
    assert m[players[2]["key"]]["status"] == "QUESTIONABLE"  # NYK not in report -> ESPN
    stale = injuries.merge(players, src, "2026-11-16")  # yesterday's report is ignored
    assert stale[players[0]["key"]]["source"] == "ESPN" or stale[players[0]["key"]]["source"] == "DK"


def test_notification_text():
    changes = [{"name": "A B", "team": "BOS", "from": "QUESTIONABLE", "to": "OUT", "reason": "Knee"}]
    msgs = notify.injury_messages(changes, {"BOS": [("C D", 30.0, 36.5)]})
    assert msgs[0]["title"] == "🏀 A B (BOS) → OUT"
    assert "Questionable → OUT (Knee)" in msgs[0]["message"]
    assert "C D 30.0→36.5" in msgs[0]["message"] and msgs[0]["priority"] == 5


# ── starting lineups ─────────────────────────────────────────────────────────
def test_lineups_feed_parsing():
    from pipeline.sources import lineups
    def team(abbr, status, names):
        ps = [{"playerName": n, "position": p, "lineupStatus": status, "rosterStatus": "Active"}
              for n, p in zip(names, ["PG", "SG", "SF", "PF", "C"])]
        ps.append({"playerName": "Bench Guy", "position": "", "lineupStatus": status, "rosterStatus": "Active"})
        return {"teamAbbreviation": abbr, "players": ps}
    data = {"games": [{"gameId": "1", "homeTeam": team("LAL", "Confirmed", list("ABCDE")),
                       "awayTeam": team("GS", "Expected", list("FGHIJ"))}]}
    out = lineups.parse(data)
    assert out["LAL"]["status"] == "confirmed" and out["LAL"]["starters"] == list("ABCDE")
    assert out["GSW"]["status"] == "expected"


def test_projected_starters_and_unexpected_starter(monkeypatch):
    players = _slate_players(monkeypatch)
    logs = syn.season_logs(TODAY)
    base = {r["name"]: r for r in model.project(players, logs, model.Context(today=TODAY))}
    # with no announcement Sigma projects the usual five
    assert {n for n, r in base.items() if r["team"] == "BOS" and r["is_starter"]} == {syn.name("BOS", i) for i in range(5)}
    assert base[syn.name("BOS", 0)]["starter_status"] == "projected"
    # star out -> his same-position backup is projected to start
    star = next(p for p in players if p["name"] == syn.name("BOS", 0))
    out = {r["name"]: r for r in model.project(players, logs, model.Context(
        today=TODAY, statuses={star["key"]: {"status": "OUT"}}))}
    assert out[syn.name("BOS", 5)]["is_starter"]
    # confirmed lineup with a surprise: bench player J starts over D
    five = [syn.name("BOS", i) for i in (0, 1, 2, 4, 8)]
    conf = {r["name"]: r for r in model.project(players, logs, model.Context(
        today=TODAY, lineups={"BOS": {"status": "confirmed", "starters": five}}))}
    j, d = syn.name("BOS", 8), syn.name("BOS", 3)
    assert conf[j]["unexpected"] == "starting" and conf[d]["unexpected"] == "benched"
    assert conf[j]["proj_minutes"] > base[j]["proj_minutes"] + 4
    assert conf[d]["proj_minutes"] < base[d]["proj_minutes"] - 4
    assert conf[j]["starter_status"] == "confirmed"


def test_lineup_change_events():
    old = {"BOS": {"status": "expected", "starters": list("ABCDE")},
           "LAL": {"status": "confirmed", "starters": list("FGHIJ")}}
    new = {"BOS": {"status": "confirmed", "starters": list("ABCDE")},
           "LAL": {"status": "confirmed", "starters": list("FGHIK")},
           "NYK": {"status": "expected", "starters": list("LMNOP")},
           "GSW": {"status": "confirmed", "starters": list("QRSTU")}}
    ev = {e["team"]: e for e in notify.lineup_changes(old, new, locked_teams={"GSW"})}
    assert ev["BOS"]["kind"] == "confirmed"
    assert ev["LAL"]["kind"] == "changed" and ev["LAL"]["in"] == ["K"] and ev["LAL"]["out"] == ["J"]
    assert ev["NYK"]["kind"] == "expected" and "GSW" not in ev
    msgs = notify.lineup_messages([ev["LAL"]], {"LAL": ["K starting (rarely starts)"]}, {})
    assert "CHANGED" in msgs[0]["title"] and "In: K" in msgs[0]["message"] and msgs[0]["priority"] == 5


def test_live_progress():
    assert nba_cdn.live_progress({"game": {"gameStatus": 2, "period": 3, "gameClock": "PT06M00.00S"}})[0] == pytest.approx(30 / 48)
    assert nba_cdn.live_progress({"game": {"gameStatus": 3, "period": 4}})[0] == 1.0
    assert nba_cdn.live_progress({"game": {"gameStatus": 1, "period": 0}})[0] == 0.0
    assert nba_cdn.live_progress({"game": {"gameStatus": 2, "period": 5, "gameClock": "PT03M00.00S"}})[0] == 1.0


def test_only_classic_and_showdown_contest_types(monkeypatch):
    now = syn.utc(2026, 11, 15, 18)
    lobby = syn.lobby(TIP)
    start = lobby["DraftGroups"][0]["StartDate"]
    for dg, ct, suffix in ((600, 73, " (NBA Tiers)"), (601, 343, " (Points)"), (602, 112, " (2H BOS @ LAL)"), (603, 188, None)):
        lobby["DraftGroups"].append({"DraftGroupId": dg, "ContestTypeId": ct, "DraftGroupTag": "Featured",
                                     "StartDate": start, "GameCount": 2, "ContestStartTimeSuffix": suffix})
    monkeypatch.setattr(draftkings, "get_json", lambda url: lobby)
    assert [s.draft_group_id for s in draftkings.list_draft_groups(now)] == [555, 556]
