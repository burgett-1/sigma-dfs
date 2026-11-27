"""Python half of the shared conformance fixture (the JS half is web/tests/sports.test.mjs)."""
import json
from pathlib import Path

import numpy as np
import pytest

from pipeline import sports
from pipeline.sports import corr, slots

FIX = json.loads((Path(__file__).parent / "fixtures" / "sports" / "conformance.json").read_text())


def test_fixture_sizes():
    assert len(FIX["eligibility"]) >= 20 and len(FIX["legality"]) >= 15 and len(FIX["malformed_registries"]) == 6


@pytest.mark.parametrize("case", FIX["eligibility"], ids=lambda c: f"{c['sport']}-{c['kind']}-{c['roster_position']}")
def test_eligibility(case):
    el = slots.eligibility(case["roster_position"], case["position"], slots.spec(case["sport"], case["kind"]))
    assert (el["slots"] if el else None) == case["slots"]


@pytest.mark.parametrize("case", FIX["legality"], ids=lambda c: f"{c['sport']}-{c['label']}")
def test_legality(case):
    sp = slots.spec(case["sport"], case["kind"])
    players = [{**p, "code": slots.pos_code(p["pos"], sp)} for p in case["players"]]
    assert slots.lineup_legal(players, sp) is case["legal"]


def test_implied_correlation_matrix():
    c = FIX["implied_corr"]
    assert np.allclose(corr.implied_corr(c["players"], c["sport"]), np.array(c["matrix"]), atol=c["tol"], rtol=0)


@pytest.mark.parametrize("case", FIX["entries_headers"], ids=lambda c: str(c["expect"]))
def test_entries_header_detection(case):
    assert slots.detect_entries_sport(case["header"]) == case["expect"]


@pytest.mark.parametrize("case", FIX["malformed_registries"], ids=lambda c: c["label"])
def test_malformed_registry_rejected(case):
    assert sports.validate(case["registry"]), case["label"]
