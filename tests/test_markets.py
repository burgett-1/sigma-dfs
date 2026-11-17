from datetime import date, datetime, timezone

import pandas as pd
import pytest

from pipeline import markets, model
from pipeline.names import norm_name, player_key
from pipeline.sources import odds_api
from tests import synthetic as syn
from tests.test_pipeline import TIP, TODAY, _slate_players


def test_odds_math():
    assert odds_api.american_to_prob(-110) == pytest.approx(0.5238, abs=1e-4)
    assert odds_api.american_to_prob(150) == pytest.approx(0.4)
    assert odds_api.devig(0.5238, 0.5238) == pytest.approx(0.5)
    assert odds_api.implied_mean(27.5, 0.5, 6) == pytest.approx(27.5)
    assert odds_api.implied_mean(27.5, 0.6, 6) > 28.9          # 60% over -> market expects ~29


GAME = {"id": "ev1", "commence_time": "2026-11-16T00:30:00Z", "home_team": "Los Angeles Lakers",
        "away_team": "Boston Celtics", "bookmakers": [
            {"key": "pinnacle", "title": "Pinnacle", "markets": [
                {"key": "spreads", "outcomes": [{"name": "Los Angeles Lakers", "price": -105, "point": 4.5},
                                                {"name": "Boston Celtics", "price": -105, "point": -4.5}]},
                {"key": "totals", "outcomes": [{"name": "Over", "price": -105, "point": 228.5},
                                               {"name": "Under", "price": -105, "point": 228.5}]},
                {"key": "h2h", "outcomes": [{"name": "Los Angeles Lakers", "price": 160},
                                            {"name": "Boston Celtics", "price": -180}]}]},
            {"key": "draftkings", "title": "DraftKings", "markets": [
                {"key": "totals", "outcomes": [{"name": "Over", "price": -110, "point": 229.5},
                                               {"name": "Under", "price": -110, "point": 229.5}]}]}]}


def test_game_lines_and_team_totals():
    g = odds_api.parse_game(GAME)
    assert g["home"] == "LAL" and g["away"] == "BOS" and g["sharpest"] == "pinnacle"
    assert g["home_margin"] == pytest.approx(-4.5, abs=0.01)       # Lakers +4.5 at even money
    assert 228.5 <= g["total"] <= 229.5                             # Pinnacle weighted most
    t = odds_api.team_odds([g])
    assert t["BOS"]["spread"] == pytest.approx(-4.5, abs=0.01) and t["BOS"]["implied"] > t["LAL"]["implied"]
    assert t["BOS"]["implied"] + t["LAL"]["implied"] == pytest.approx(g["total"])


def _props_event(name, over_price, line=27.5):
    return {"bookmakers": [{"key": "pinnacle", "markets": [
        {"key": "player_points", "outcomes": [
            {"name": "Over", "description": name, "price": over_price, "point": line},
            {"name": "Under", "description": name, "price": -over_price if over_price < 0 else -200, "point": line}]},
        {"key": "player_rebounds", "outcomes": [
            {"name": "Over", "description": name, "price": -110, "point": 8.5},
            {"name": "Under", "description": name, "price": -110, "point": 8.5}]}]}]}


def test_props_parse_and_projection_follows_the_market(monkeypatch):
    star = syn.name("BOS", 0)
    p = odds_api.parse_props(_props_event(star, -110, 40.5))
    assert p[norm_name(star)]["pts"]["line"] == 40.5 and p[norm_name(star)]["reb"]["over_fair"] == pytest.approx(0.5)
    players = _slate_players(monkeypatch)
    logs = syn.season_logs(TODAY)
    base = {r["name"]: r for r in model.project(players, logs, model.Context(today=TODAY))}
    hi = odds_api.parse_props(_props_event(star, -250, 40.5))      # market strongly on the over
    lo = odds_api.parse_props(_props_event(star, 250, 15.5))       # market well below the model (~21 pts)
    r_hi = {r["name"]: r for r in model.project(players, logs, model.Context(today=TODAY, props=hi))}[star]
    r_lo = {r["name"]: r for r in model.project(players, logs, model.Context(today=TODAY, props=lo))}[star]
    assert r_hi["market_stats"] == 2 and r_lo["market_stats"] == 2
    assert r_hi["proj"] > base[star]["proj"] > r_lo["proj"]        # projections move with the odds
    assert r_hi["proj_model"] == pytest.approx(base[star]["proj"])
    other = syn.name("LAL", 0)
    assert {r["name"]: r for r in model.project(players, logs, model.Context(today=TODAY, props=hi))}[other]["proj"] == \
        pytest.approx(base[other]["proj"])                          # players without props unchanged
    rows = markets.prop_rows(list(base.values()), hi)
    assert rows and rows[0]["player"] == star and {"edge_over", "model_over", "over_fair"} <= set(rows[0])


def test_team_history_ats_ou():
    rows = []
    for i, (ts, os_, sp, tot) in enumerate([(110, 100, -5.5, 205.5), (100, 104, -2.0, 210.0), (120, 110, 3.0, 225.0)]):
        rows.append({"game_id": f"g{i}", "date": f"2026-11-0{i + 1}", "team": "BOS", "opp": "NYK", "home": i % 2 == 0,
                     "team_score": ts, "opp_score": os_, "spread": sp, "total": tot})
    h = markets.team_history(pd.DataFrame(rows), 2026)["BOS"]
    # 110-100 vs -5.5 covers; 100-104 vs -2 loses; 120-110 vs +3 covers
    assert h["ats"] == "2-1" and h["ou"] == "2-1" and h["games"] == 3
    assert h["recent"][-1]["ats"] == "W"
