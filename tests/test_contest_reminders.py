"""Contest-result export reminders (pipeline/contest_reminders.py). notify.send is always faked."""
import gzip
import json
import os
from datetime import datetime

import pytest

from pipeline import contest_reminders as cr, contests, history, notify
from pipeline.config import ET


class _Sent(list):
    dl = None


@pytest.fixture
def sent(tmp_path, monkeypatch):
    out = _Sent()
    dl = tmp_path / "dl"
    dl.mkdir()
    monkeypatch.setenv("SIGMA_CONTEST_IMPORT", str(dl))
    monkeypatch.setattr(history, "HISTORY_DIR", tmp_path / "history")
    monkeypatch.setenv("NTFY_TOPIC", "test-topic-not-real")

    def fake_send(title, message, priority=3, tags=None, click=None, actions=None, markdown=False):
        out.append({"title": title, "message": message, "click": click, "actions": actions, "markdown": markdown})
        return True
    monkeypatch.setattr(notify, "send", fake_send)
    out.dl = dl
    return out


def _history(day):
    p = history.HISTORY_DIR / f"{day}.csv.gz"
    p.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(p, "wt") as f:
        f.write("game_id,date,team\n1,%s,BOS\n" % day)


def _et(y, m, d, h, mi=0):
    return datetime(y, m, d, h, mi, tzinfo=ET)


ENTRY_UNDERSCORE = ("Sport,Game_Type,Entry_Key,Entry,Contest_Key,Contest_Date_EST,Place,Points\n"
                    "NBA,Classic,1,NBA $5 Slam,198000001,2026-11-25 19:00:00,10,300\n"
                    "NBA,Classic,2,NBA $5 Slam,198000001,2026-11-25 19:00:00,12,290\n"
                    "NFL,Classic,3,NFL Sunday,198000002,2026-11-23 13:00:00,1,200\n"
                    "NBA,Showdown,4,NBA SD,198000003,2026-11-18 19:30:00,5,100\n")
ENTRY_SPACES = "﻿SPORT,Entry,Contest Key,Contest Date\nnba,Late Night,198000009,11/25/2026 10:00 PM\nMLB,x,5,11/25/2026\n"


def test_parse_entry_history_variants():
    a = cr.parse_entry_history(ENTRY_UNDERSCORE)
    assert set(a) == {"198000001", "198000003"}                          # NBA only, deduped
    assert a["198000001"] == {"date": "2026-11-25", "name": "NBA $5 Slam"}
    b = cr.parse_entry_history(ENTRY_SPACES)
    assert b == {"198000009": {"date": "2026-11-25", "name": "Late Night"}}
    assert cr.parse_entry_history("") == {} and cr.parse_entry_history("a,b\n1,2\n") == {}


def test_morning_after_sends_once_at_nine(sent):
    _history("2026-11-25")
    (sent.dl / "draftkings-contest-entry-history.csv").write_text(ENTRY_UNDERSCORE)
    assert cr.check(_et(2026, 11, 26, 8, 59)) == [] and sent == []
    # the 11-18 showdown is 8 days old and not imported, so its follow-up goes out too
    assert cr.check(_et(2026, 11, 26, 9, 0)) == ["morning:2026-11-25", "followup:1"]
    assert cr.check(_et(2026, 11, 26, 15, 0)) == []                     # once per DFS day
    assert len(sent) == 2
    m = sent[0]
    assert "198000003" not in m["message"] and "198000003" in sent[1]["message"]
    assert m["title"] == "Export yesterday's NBA contest results (1)" and m["markdown"] is True
    assert "exportfullstandingscsv/198000001" in m["message"] and "gamecenter/198000001" in m["message"]
    assert "10 days" in m["message"]
    assert len(m["actions"]) <= 3 and all(a["action"] == "view" for a in m["actions"])
    assert (sent.dl / "draftkings-contest-entry-history.csv").exists()    # raw file left in place


def test_generic_when_no_ids(sent):
    _history("2026-11-25")
    assert cr.check(_et(2026, 11, 26, 9, 5)) == ["morning:2026-11-25"]
    assert "mycontests" in sent[0]["message"] and sent[0]["click"] == cr.MY_CONTESTS_URL


def test_no_slate_no_reminder(sent):
    assert cr.check(_et(2026, 11, 26, 10)) == [] and sent == []


def test_imported_contests_not_reminded_and_followup(sent):
    (sent.dl / "draftkings-contest-entry-history.csv").write_text(
        "Sport,Entry,Contest_Key,Contest_Date_EST\n"
        "NBA,A,198000011,2026-11-18 19:00\nNBA,B,198000012,2026-11-18 19:00\nNBA,Old,198000013,2026-11-10 19:00\n")
    contests.CONTESTS.mkdir(parents=True, exist_ok=True)
    (contests.CONTESTS / "index.json").write_text(json.dumps({"198000011": {"file_date": "2026-11-19"}}))
    out = cr.check(_et(2026, 11, 25, 9, 30))                            # day 7 after the 18th
    assert "followup:1" in out
    f = next(m for m in sent if m["title"].startswith("Last chance"))
    assert "198000012" in f["message"] and "198000011" not in f["message"] and "198000013" not in f["message"]
    n = len(sent)
    cr.check(_et(2026, 11, 26, 9, 30))
    assert not any(m["title"].startswith("Last chance") for m in sent[n:])   # one follow-up per contest


def test_nothing_older_than_ten_days(sent):
    (sent.dl / "draftkings-contest-entry-history.csv").write_text(
        "Sport,Entry,Contest_Key,Contest_Date_EST\nNBA,Old,198000021,2026-11-10 19:00\n")
    assert cr.check(_et(2026, 11, 25, 9, 30)) == [] and sent == []


def test_no_ntfy_topic_marks_nothing(sent, monkeypatch):
    monkeypatch.delenv("NTFY_TOPIC")
    monkeypatch.setattr(notify, "send", lambda *a, **k: False)            # what send returns when disabled
    _history("2026-11-25")
    assert cr.check(_et(2026, 11, 26, 9, 0)) == []
    assert not (contests.PRIVATE / "reminders.json").exists()


def test_module_makes_no_requests():
    src = open(cr.__file__).read()
    assert "session(" not in src and "import requests" not in src and "urlopen" not in src


def test_notify_send_payload(monkeypatch):
    posted = []

    class S:
        def post(self, url, json=None, headers=None, timeout=None):
            posted.append(json)

            class R:
                def raise_for_status(self):
                    pass
            return R()
    monkeypatch.setattr(notify, "session", lambda: S())
    monkeypatch.setenv("NTFY_TOPIC", "t")
    notify.send("a", "b", 3, ["x"], click="https://c")
    assert posted[-1] == {"topic": "t", "title": "a", "message": "b", "priority": 3, "tags": ["x"], "click": "https://c"}
    acts = [{"action": "view", "label": str(i), "url": "https://u"} for i in range(5)]
    notify.send("a", "b", actions=acts, markdown=True)
    assert posted[-1]["actions"] == acts[:3] and posted[-1]["markdown"] is True
    assert os.environ.get("NTFY_TOPIC") == "t"
