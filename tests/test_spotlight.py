from datetime import date

from pipeline import model, spotlight
from tests.test_factors import league


def _hist():
    return model.prepare_history(league(seed=1), date(2026, 6, 1))


def test_same_situation_card_finds_the_usage_boost():
    h = _hist()
    pid = lambda n: h.loc[h["name"] == n, "person_id"].iloc[0]
    rows = [
        {"name": "BOS P0", "team": "BOS", "team_dk": "BOS", "status": "OUT", "fresh": 1.0, "minutes": 34,
         "person_id": pid("BOS P0"), "proj": 0, "salary": 10000},
        {"name": "BOS P1", "team": "BOS", "team_dk": "BOS", "status": "", "fresh": 1.0, "minutes": 32,
         "person_id": pid("BOS P1"), "proj": 40, "salary": 7000, "own": 20},
        {"name": "BOS P5", "team": "BOS", "team_dk": "BOS", "status": "", "fresh": 1.0, "minutes": 20,
         "person_id": pid("BOS P5"), "proj": 20, "salary": 4000, "own": 5},
    ]
    cards = spotlight.with_without_cards(rows, h, season=2025)
    names = [c["player"] for c in cards]
    assert "BOS P1" in names and "BOS P5" not in names     # only P1 truly gains (+25%) when P0 sits
    c = cards[names.index("BOS P1")]
    assert c["evidence"]["avg_without"] > c["evidence"]["avg_with"] * 1.1 and c["evidence"]["z"] >= 2.5
    assert "without BOS P0" in c["headline"]


def test_tag_exposure_and_profile_cards():
    model_ = {"large": {"table": {"1-3|1": {"exp": 45.0, "n": 50}, "1-3|0": {"exp": 30.0, "n": 50},
                                  "4-6|0": {"exp": 12.0, "n": 50}, "4-6|1": {"exp": 20.0, "n": 50}},
                        "slates": 100}}
    rows = [{"name": f"P{i}", "team": "A" if i < 3 else "B", "team_dk": "A", "salary": 5000, "proj": 40 - i,
             "own": 10.0, "status": "", "fresh": 1, "minutes": 30} for i in range(5)]
    rows.append({"name": "Hurt", "team": "A", "team_dk": "A", "salary": 9000, "proj": 0, "own": 0,
                 "status": "OUT", "fresh": 1, "minutes": 33})
    spotlight.tag_exposure(rows, 9, model_)
    assert rows[0]["value_rank"] == 1 and rows[0]["teammate_out"] and rows[0]["opt_exposure"] == 45.0
    assert rows[3]["opt_exposure"] == 12.0 and rows[5]["opt_exposure"] == 0.0
    cards = spotlight.profile_cards(rows, 9, model_, {})
    assert cards[0]["player"] == "P0" and cards[0]["strong"]
