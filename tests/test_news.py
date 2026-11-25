import json
from datetime import datetime, timedelta, timezone

import pytest

from pipeline import contests, news


@pytest.fixture
def priv(tmp_path, monkeypatch):
    monkeypatch.setattr(news, "ITEMS", tmp_path / "x_items.json")
    monkeypatch.setattr(news, "SPEED", tmp_path / "speed.json")
    return tmp_path


def _item(i, at, status, key="kat|NYK", confident=True):
    return {"id": str(i), "text": f"Karl-Anthony Towns {status}", "at": at.isoformat(), "account": "Newzgod_dfs",
            "seen_at": at.isoformat(), "players": [key], "names": ["Karl-Anthony Towns"], "status": status,
            "confident": confident}


def test_post_applies_until_official_changes(priv):
    t0 = datetime(2026, 11, 25, 22, 0, tzinfo=timezone.utc)
    state = {}
    base = {"kat|NYK": {"status": "QUESTIONABLE", "reason": "ankle", "source": "NBA"}}
    news.apply(base, state, t0)                                   # baseline
    news.record([_item(1, t0 + timedelta(minutes=1), "OUT")])
    out = news.apply(base, state, t0 + timedelta(minutes=2))
    assert out["kat|NYK"]["status"] == "OUT" and out["kat|NYK"]["source"] == "X @Newzgod_dfs"
    # the official report confirms 4 minutes after the post: speed is logged, status stays OUT
    base2 = {"kat|NYK": {"status": "OUT", "reason": "ankle", "source": "NBA"}}
    out = news.apply(base2, state, t0 + timedelta(minutes=5))
    assert out["kat|NYK"]["status"] == "OUT"
    sp = json.loads(news.SPEED.read_text())
    assert len(sp) == 1 and sp[0]["lead_seconds"] == 240 and sp[0]["official_source"] == "NBA"
    # later the official side changes again (newer than the post): official wins
    base3 = {"kat|NYK": {"status": "", "reason": "", "source": "NBA"}}
    out = news.apply(base3, state, t0 + timedelta(minutes=30))
    assert out["kat|NYK"]["status"] == ""


def test_ambiguous_or_old_posts_do_nothing(priv):
    t0 = datetime(2026, 11, 25, 22, 0, tzinfo=timezone.utc)
    state = {}
    base = {"kat|NYK": {"status": "QUESTIONABLE", "reason": "", "source": "NBA"}}
    news.apply(base, state, t0)
    news.record([_item(1, t0 + timedelta(minutes=1), None, confident=False),
                 _item(2, t0 - timedelta(days=1), "OUT")])          # yesterday's post
    assert news.apply(base, state, t0 + timedelta(minutes=2))["kat|NYK"]["status"] == "QUESTIONABLE"


def test_private_folder():
    assert str(news.NEWS_DIR).startswith(str(contests.PRIVATE))
