"""Sport registry: validation, NBA entry == today's constants, registry-built NBA
slot spec == the legacy sim literals, registry scorer == history.dk_points."""
import copy
import json
from datetime import date

import pytest

from pipeline import config, history, sim, sports
from pipeline.sports import scoring, slots
from tests import synthetic as syn


def test_registry_validates_and_has_every_dk_sport():
    assert sports.validate(sports.REGISTRY) == []
    assert set(sports.codes()) == {"NBA", "NFL", "NHL", "CFB", "MLB", "GOLF", "MMA", "NAS"}
    assert [c for c in sports.codes() if sports.get(c)["enabled"]] == ["NBA", "NFL"]


def test_nba_entry_equals_config_constants():
    nba = sports.get("NBA")
    t = nba["scoring"]["tables"]["player"]
    assert t["per_stat"] == config.DK_SCORING
    assert [m["pts"] for m in t["multi_cat"]] == [config.DK_DOUBLE_DOUBLE, config.DK_TRIPLE_DOUBLE]
    cl = nba["kinds"]["classic"]
    assert cl["cap"] == config.DK_SALARY_CAP
    assert [s["label"] for s in cl["slots"]] == config.DK_ROSTER_SLOTS
    assert {s["label"]: set(s["pos"]) for s in cl["slots"]} == config.DK_SLOT_OK
    assert nba["play_prob"] == config.SIM["play_prob"]
    assert nba["corr"]["default"] == config.SIM["default_corr"]
    assert cl["field_min_salary"] == config.SIM["field_min_salary"]
    assert nba["projection"]["min_proj"] == config.SIM["min_proj"]
    assert nba["scoring"]["verified"] is True and nba["data_prefix"] == ""


def test_nba_slot_spec_reproduces_legacy_literals():
    sp = slots.spec("NBA", "classic")
    assert sp.positions == ("PG", "SG", "SF", "PF", "C")
    assert dict(zip(sp.labels, sp.masks)) == {"PG": 1, "SG": 2, "SF": 4, "PF": 8, "C": 16, "G": 3, "F": 12, "UTIL": 31}
    assert sim.POS == ("PG", "SG", "SF", "PF", "C") and sim.SLOTS == ("PG", "SG", "SF", "PF", "C", "G", "F", "UTIL")
    legacy = {U: sum(1 for sl in sim.SLOTS if (sim.SLOT_MASK[sl] & ~U) == 0) for U in range(1, 31)}
    assert sim.HALL_NEED == legacy == sp.hall_need
    sd = slots.spec("NBA", "showdown")
    assert sd.labels == ("CPT",) + ("UTIL",) * 5 and sd.cpt_mult == 1.5 and set(sd.masks) == {31}


def test_registry_scorer_equals_history_dk_points():
    logs = syn.season_logs(date(2026, 11, 15))
    table = scoring.table_for("NBA")
    assert list(scoring.score(logs, table)) == list(history.dk_points(logs))


def test_enabled_honours_kill_switch():
    assert sports.enabled(env="") == ["NBA", "NFL"]
    assert sports.enabled(env="NBA") == ["NBA"]
    assert sports.enabled(env="nba, nfl ,XYZ") == ["NBA", "NFL"]


def test_in_season_wraps_the_new_year():
    assert sports.in_season("NFL", date(2026, 11, 26)) and sports.in_season("NFL", date(2027, 2, 1))
    assert not sports.in_season("NFL", date(2027, 6, 1))
    assert not sports.in_season("NAS", date(2026, 11, 26))


def test_validator_rejects_bad_entries(tmp_path):
    bad = copy.deepcopy(sports.REGISTRY)
    bad["sports"]["NFL"]["kinds"]["showdown"]["slots"][1]["mult"] = 1.5
    bad["sports"]["NFL"]["corr"]["loadings"]["QB"]["game"] = 0.9
    errs = sports.validate(bad)
    assert any("exactly one multiplier" in e for e in errs) and any("sum of squares" in e for e in errs)
    p = tmp_path / "registry.json"
    p.write_text(json.dumps(bad))
    with pytest.raises(sports.RegistryError):
        sports.load(p)
