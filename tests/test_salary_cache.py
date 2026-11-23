import pytest

from pipeline import config
from pipeline.sources import draftkings as dk

CSV = ("Position,Name + ID,Name,ID,Roster Position,Salary,Game Info,TeamAbbrev,AvgPointsPerGame\n"
       "PG,Alpha One (101),Alpha One,101,PG/G/UTIL,9000,BOS@NYK 11/23/2026 07:30PM ET,BOS,45.1\n"
       "C,Bravo Two (102),Bravo Two,102,C/UTIL,7000,BOS@NYK 11/23/2026 07:30PM ET,NYK,38.0\n")


def test_network_drop_uses_saved_salaries(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "STATE_DIR", tmp_path)
    slate = dk.Slate(draft_group_id=157216, name="Main", start_utc=None, game_count=1, featured=False)
    monkeypatch.setattr(dk, "get_text", lambda url: CSV)
    assert len(dk.load_draftables(slate).players) == 2

    def down(url):
        raise ConnectionError("NameResolutionError: www.draftkings.com")
    monkeypatch.setattr(dk, "get_text", down)
    again = dk.load_draftables(dk.Slate(draft_group_id=157216, name="Main", start_utc=None, game_count=1, featured=False))
    assert [p["name"] for p in again.players] == ["Alpha One", "Bravo Two"]
    # a group never seen before still fails (nothing saved to fall back on)
    with pytest.raises(ConnectionError):
        dk.load_draftables(dk.Slate(draft_group_id=999, name="X", start_utc=None, game_count=1, featured=False))
