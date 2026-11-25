from pipeline import live
from pipeline.sources import espn


def test_news_watch_fires_on_change(monkeypatch):
    w = live.NewsWatch()
    w.EVERY = 0
    state = {"fp": "a"}
    monkeypatch.setattr(w, "fingerprint", lambda: state["fp"])
    assert w.changed() is False          # first look is the baseline
    assert w.changed() is False          # nothing new
    state["fp"] = "b"
    assert w.changed() is True           # a status / lineup / report change
    assert w.changed() is False


def test_espn_day_to_day_reads_the_note():
    n = espn.normalize_status
    assert n("Day-To-Day", "James is probable for Wednesday's game against the Bulls.") == "PROBABLE"
    assert n("Day-To-Day", "Smith (illness) is listed as questionable.") == "QUESTIONABLE"
    assert n("Day-To-Day", "Doubtful to play Friday.") == "DOUBTFUL"
    assert n("Day-To-Day", "") == "QUESTIONABLE"
    assert n("Out", "was probable earlier") == "OUT"
