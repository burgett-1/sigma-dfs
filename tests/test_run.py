"""End-to-end: three 15-minute runs with all network sources faked."""
import csv
import json

import pytest

from pipeline import history, injuries, notify, run as runmod
from pipeline.sources import draftkings, espn, injury_report, nba_cdn
from tests import synthetic as syn
from tests.test_pipeline import TIP, TODAY


@pytest.fixture
def env(tmp_path, monkeypatch):
    for mod, attrs in ((runmod, ("DATA_DIR", "STATE_DIR", "SLATES_DIR")),):
        monkeypatch.setattr(mod, "DATA_DIR", tmp_path)
        monkeypatch.setattr(mod, "STATE_DIR", tmp_path / "state")
        monkeypatch.setattr(mod, "SLATES_DIR", tmp_path / "slates")
    monkeypatch.setattr(history, "HISTORY_DIR", tmp_path / "history")
    history.save(syn.season_logs(TODAY))

    monkeypatch.setattr(draftkings, "get_json",
                        lambda url: syn.lobby(TIP) if "lobby" in url else syn.draftables(TIP))
    monkeypatch.setattr(nba_cdn, "schedule", lambda: [])
    monkeypatch.setattr(history, "backfill_season", lambda logs, y: logs)
    monkeypatch.setattr(espn, "odds_for_date", lambda d: {
        "BOS": {"implied": 116.0, "spread": -4.0}, "LAL": {"implied": 112.0, "spread": 4.0},
        "GSW": {"implied": 114.0, "spread": 1.0}, "NYK": {"implied": 115.0, "spread": -1.0}})
    monkeypatch.setattr(espn, "injuries", lambda: [])

    report = {"rows": []}
    monkeypatch.setattr(injury_report, "find_latest", lambda now, last=None: ("u", b"%PDF"))
    monkeypatch.setattr(injury_report, "parse_pdf", lambda body: report["rows"])

    sent = []
    monkeypatch.setattr(notify, "send", lambda title, msg, *a, **k: sent.append((title, msg)) or True)
    monkeypatch.setattr(notify.time, "sleep", lambda s: None)
    return tmp_path, report, sent


def _row(name, team, status):
    return {"name": name, "team": team, "status": status, "reason": "Knee", "game_date": "11/15/2026"}


def _csv(path):
    with open(path) as f:
        return list(csv.reader(f))


def test_three_runs(env):
    tmp, report, sent = env
    star, bos_b = syn.name("BOS", 0), syn.name("BOS", 1)
    lal, gsw = syn.name("LAL", 0), syn.name("GSW", 0)

    # run 1: 1pm ET, BOS star questionable -> baseline message only
    report["rows"] = [_row(star, "BOS", "QUESTIONABLE"), {"team": "LAL", "name": None},
                      {"team": "GSW", "name": None}, {"team": "NYK", "name": None}]
    report["rows"] = [r for r in report["rows"] if r.get("name")] + [
        _row("Someone Else", "LAL", "OUT"), _row("Someone Else", "GSW", "OUT"),
        _row("Someone Else", "NYK", "OUT")]
    assert runmod.run(syn.utc(2026, 11, 15, 18)) == 0
    assert len(sent) == 1 and "live" in sent[0][0]
    rows = _csv(tmp / "projections.csv")
    assert rows[0][:8] == runmod.CSV_HEADER[:8] and len(rows) == 41
    assert all(len(r) == 21 for r in rows)
    by = {r[0]: r for r in rows[1:]}
    assert by[star][18] == "Questionable" and float(by[star][7]) > 30
    assert by[gsw][3] == "GS" and by[gsw][4] == "NY"  # DK abbreviations kept
    assert (tmp / "slates" / "555.csv").exists()
    b_before = float(by[bos_b][7])

    # run 2: 1:15pm ET, star ruled OUT -> one push with projection moves
    sent.clear()
    report["rows"][0] = _row(star, "BOS", "OUT")
    runmod.run(syn.utc(2026, 11, 15, 18, 15))
    assert len(sent) == 1
    title, msg = sent[0]
    assert star in title and "OUT" in title and "Questionable → OUT" in msg
    assert bos_b in msg  # teammate projection move is listed
    by = {r[0]: r for r in _csv(tmp / "projections.csv")[1:]}
    assert float(by[star][7]) == 0 and by[star][18] == "Out"
    assert float(by[bos_b][7]) > b_before + 2
    log = json.loads((tmp / "injury_log.json").read_text())
    assert log[0]["name"] == star and log[0]["to"] == "OUT"

    # run 3: after BOS@LAL tips. LAL star ruled out late and a GSW change.
    sent.clear()
    gsw_before = float(by[gsw][7])
    lal_before = float(by[lal][7])
    report["rows"] += [_row(lal, "LAL", "OUT"), _row(gsw, "GSW", "OUT")]
    runmod.run(syn.utc(2026, 11, 16, 0, 45))
    by = {r[0]: r for r in _csv(tmp / "projections.csv")[1:]}
    assert float(by[lal][7]) == pytest.approx(lal_before)   # frozen at lock
    assert float(by[gsw][7]) == 0 and gsw_before > 0         # unlocked game still updates
    assert len(sent) == 1 and gsw in sent[0][0]              # no alert for the locked game
    meta = json.loads((tmp / "meta.json").read_text())
    assert meta["frozen_players"] == 20 and meta["next_lock_utc"].startswith("2026-11-16T02:30")
    slates = json.loads((tmp / "slates.json").read_text())
    assert slates[0]["games"][0]["locked"] and not slates[0]["games"][1]["locked"]

    # run 4: everything locked -> final run, then later runs are no-ops
    runmod.run(syn.utc(2026, 11, 16, 3, 0))
    state = json.loads((tmp / "state" / "run_state.json").read_text())
    assert state["final_run_for"] == "2026-11-15"
    stamp = (tmp / "meta.json").read_text()
    runmod.run(syn.utc(2026, 11, 16, 3, 15))
    assert (tmp / "meta.json").read_text() == stamp
