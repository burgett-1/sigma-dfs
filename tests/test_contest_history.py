"""Tests for pipeline/contest_history.py, the public aggregate-only contest DB.

Synthetic CSVs are written to tmp_path. Real-data tests read the DK exports in
place from ~/Downloads/DK DFS Post Contest Results and are skipped when absent.
Nothing is written into the repo's data/ or ~/SigmaDFS-Private.
"""
import csv
import io
import json
import math
import re
import zipfile
from pathlib import Path

import pytest

from pipeline import contest_history as H
from pipeline import contests as C
from pipeline.names import norm_name

REAL_DIR = Path.home() / "Downloads" / "DK DFS Post Contest Results"
REAL_SNAPS = Path.home() / "SigmaDFS-Private" / "projections"
HEADER = ["Rank", "EntryId", "EntryName", "TimeRemaining", "Points", "Lineup", "", "Player",
          "Roster Position", "%Drafted", "FPTS"]
NBA_SLOTS = ["PG", "SG", "SF", "PF", "C", "G", "F", "UTIL"]
NFL_SLOTS = ["QB", "RB", "RB", "WR", "WR", "WR", "TE", "FLEX", "DST"]
FORBIDDEN_KEYS = {"EntryId", "EntryName", "entry", "user", "lineup", "mine", "winner"}
NAMES = [f"Player{chr(65 + i)} Test{chr(65 + j)}" for i in range(6) for j in range(4)]  # 24 players


def quickley_file(folder):
    """The real NBA export whose player table has Quickley's PG 28.73 row (found at
    runtime so no real DraftKings contest id is written in this public repo)."""
    for f in sorted(Path(folder).glob("contest-standings-*.csv")):
        try:
            if "Immanuel Quickley,PG,28.73%" in f.read_text(encoding="utf-8-sig", errors="replace"):
                return f
        except OSError:
            continue
    return None


@pytest.fixture(autouse=True)
def _no_private_index(tmp_path, monkeypatch):
    # load_dir also looks at the private contests/index.json for file dates; keep tests hermetic
    monkeypatch.setattr(C, "CONTESTS", tmp_path / "no-private" / "contests")


def make_csv(users, slots=NBA_SLOTS, names=NAMES, player_rows=None, bom=False, points=None):
    """A DK contest-standings export. users: EntryName strings (e.g. 'zqUser (1/150)')."""
    out = io.StringIO()
    w = csv.writer(out, lineterminator="\n")
    w.writerow(HEADER)
    player_rows = player_rows if player_rows is not None else [
        (names[i], slots[i % len(slots)], f"{(40 - i) if i < 20 else 0.5:.2f}%", f"{30 - i:.2f}") for i in range(len(names))]
    n = max(len(users), len(player_rows))
    for i in range(n):
        if i < len(users):
            lu = " ".join(f"{s} {names[(i + k) % len(names)]}" for k, s in enumerate(slots))
            pts = points[i] if points else 300 - i
            left = [str(i + 1), str(900000000 + i), users[i], "0", str(pts), lu]
        else:
            left = ["", "", "", "", "", ""]
        right = list(player_rows[i]) if i < len(player_rows) else ["", "", "", ""]
        w.writerow(left + [""] + right)
    text = out.getvalue()
    return ("﻿" + text) if bom else text


def users(n, tag="zqUser"):
    return [f"{tag}{i:03d}x ({1 + i % 3}/150)" for i in range(n)]


def write(folder, cid, text, zip_=False):
    folder.mkdir(parents=True, exist_ok=True)
    if zip_:
        p = folder / f"contest-standings-{cid}.zip"
        with zipfile.ZipFile(p, "w") as z:
            z.writestr(f"contest-standings-{cid}.csv", text)
    else:
        p = folder / f"contest-standings-{cid}.csv"
        p.write_text(text, encoding="utf-8")
    return p


def snapshot(folder, day, names=NAMES, sid=1):
    d = folder / day
    d.mkdir(parents=True, exist_ok=True)
    players = {norm_name(n): [20.0 + i, max(0.5, 35.0 - i), 3000 + 300 * i] for i, n in enumerate(names)}
    (d / f"{sid}.json").write_text(json.dumps({"slate_id": sid, "date": day, "players": players}))


def all_keys(o, acc=None):
    acc = set() if acc is None else acc
    if isinstance(o, dict):
        for k, v in o.items():
            acc.add(k)
            all_keys(v, acc)
    elif isinstance(o, list):
        for v in o:
            all_keys(v, acc)
    return acc


def all_values(o, acc=None):
    """Every leaf VALUE (not schema keys)."""
    acc = [] if acc is None else acc
    if isinstance(o, dict):
        for v in o.values():
            all_values(v, acc)
    elif isinstance(o, list):
        for v in o:
            all_values(v, acc)
    else:
        acc.append(o)
    return acc


def check_schema(doc):
    assert doc["version"] == 1 and doc["sport"] == "NBA"
    assert isinstance(doc["generated_at"], str) and doc["generated_at"].endswith("Z")
    cols = doc["players"]["columns"]
    assert cols == ["contest", "name", "own", "own_by_slot", "fpts", "proj_own", "proj", "salary"]
    assert all(len(r) == len(cols) for r in doc["players"]["rows"])
    cal = doc["calibration"]
    assert len(cal["buckets"]) == 5 and all(b["n"] >= 0 for b in cal["buckets"])
    assert len(cal["by_salary"]) == 5
    assert set(cal["by_size"]) == {"single-entry", "multi"}
    keys = [c["key"] for c in doc["contests"]]
    assert keys == [f"c{i}" for i in range(1, len(keys) + 1)]
    dates = [c["date"] or "" for c in doc["contests"]]
    assert dates == sorted(dates)
    for c in doc["contests"]:
        n = c["entries_approx"]
        assert n < 100 or len(str(n).rstrip("0")) <= 2, n          # 2 significant figures
        assert c["size_bucket"] in {"single-entry", "small", "mid", "large"}
        assert c["kind"] in {"classic", "showdown"}
        assert c["date_source"] in {"snapshot", "file"}
    assert doc["samples"]["contests"] == len(doc["contests"])
    assert doc["samples"]["player_rows"] == len(doc["players"]["rows"])
    json.dumps(doc, allow_nan=False)                                # strict JSON: no NaN/Infinity
    for s in doc["skipped"]:
        assert s["reason"] in {"not_nba", "malformed", "empty", "unknown_sport"} and s["count"] >= 1


# ── schema / basic build (AC-P7) ─────────────────────────────────────────────
def test_build_schema_and_key_order(tmp_path):
    src = tmp_path / "src"
    write(src, "555000111", make_csv(users(150)))
    write(src, "555000222", make_csv(users(1234)))
    snaps = tmp_path / "snaps"
    snapshot(snaps, "2026-11-10")
    loaded = H.load_dir([src])
    # force distinct file dates so date ordering is exercised: later id gets the earlier date
    loaded[0]["file_date"], loaded[1]["file_date"] = "2026-11-20", "2026-11-12"
    doc = H.build(loaded, None)                 # no snapshots: date = file_date - 1 day
    check_schema(doc)
    assert doc["samples"]["contests"] == 2
    c1, c2 = doc["contests"]
    assert (c1["date"], c2["date"]) == ("2026-11-11", "2026-11-19")
    assert c1["entries_approx"] == 1200 and c2["entries_approx"] == 150     # 1234 -> 1200, c1 is the earlier date
    assert c1["max_entries"] == 150 and c1["size_bucket"] == "mid" and c2["size_bucket"] == "small"
    doc2 = H.build(loaded, snaps)               # with a snapshot: date = snapshot date
    check_schema(doc2)
    assert all(c["snapshot_match"] and c["date_source"] == "snapshot" and c["date"] == "2026-11-10"
               for c in doc2["contests"])
    assert all(math.isfinite(r[2]) for r in doc["players"]["rows"])


def test_sig2_rounding():
    assert [H._sig2(n) for n in (32, 99, 100, 583, 1307, 11890, 70198, 147058)] == \
        [32, 99, 100, 580, 1300, 12000, 70000, 150000]


def test_scores_and_single_entry_bucket(tmp_path):
    src = tmp_path / "src"
    us = [f"soloEntrant{i:02d}" for i in range(40)]                       # no "(k/N)": single entry
    write(src, "555000333", make_csv(us, points=[200 + i for i in range(40)]))
    doc = H.build(H.load_dir([src]), None)
    c = doc["contests"][0]
    assert c["size_bucket"] == "single-entry" and c["max_entries"] == 1
    assert c["scores"]["win"] == 239 and c["scores"]["median"] == pytest.approx(219.5)
    assert sum(c["scores"]["hist"]["counts"]) == 40
    assert c["snapshot_match"] is None and c["date_source"] == "file"
    assert all(r[5] is None and r[6] is None and r[7] is None for r in doc["players"]["rows"])


def test_slot_sum_reaches_public_rows(tmp_path):
    rows = [("Immanuel Quickley", "PG", "28.73%", "38.75"), ("Immanuel Quickley", "G", "19.73%", "38.75"),
            ("Immanuel Quickley", "UTIL", "4.17%", "38.75")] + \
           [(NAMES[i], NBA_SLOTS[i % 8], "10.00%", "20") for i in range(10)]
    src = tmp_path / "src"
    write(src, "900000123", make_csv(users(20), player_rows=rows))
    doc = H.build(H.load_dir([src]), None)
    q = [r for r in doc["players"]["rows"] if r[1] == "Immanuel Quickley"]
    assert len(q) == 1
    assert q[0][2] == pytest.approx(52.63, abs=0.01)
    assert q[0][3] == {"PG": 28.73, "G": 19.73, "UTIL": 4.17}


# ── calibration rules ────────────────────────────────────────────────────────
def test_calibration_zero_fill_only_with_good_coverage(tmp_path):
    snaps = tmp_path / "snaps"
    extra = [f"Bench{chr(65 + i)} Guy" for i in range(8)]
    snapshot(snaps, "2026-11-10", names=NAMES + extra)        # 32 snapshot players, contest has 24 -> cov 0.75
    src = tmp_path / "src"
    write(src, "555000444", make_csv(users(50)))
    doc = H.build(H.load_dir([src]), snaps)
    assert doc["contests"][0]["snapshot_match"]["coverage"] == 0.75
    assert doc["calibration"]["n"] == 32                       # 24 matched + 8 absent counted as 0%
    snaps2 = tmp_path / "snaps2"
    many = NAMES + [f"Deep{chr(65 + i)} Reserve{chr(65 + j)}" for i in range(4) for j in range(4)]  # 40 -> cov 0.6
    snapshot(snaps2, "2026-11-10", names=many + ["Extra One Guy"])                                # 41 -> cov < 0.6
    doc2 = H.build(H.load_dir([src]), snaps2)
    assert doc2["contests"][0]["snapshot_match"]["coverage"] < 0.6
    assert doc2["calibration"]["n"] == 24                      # only matched names
    assert doc2["calibration"]["fit"]["used"] is False         # n < 40


def test_no_snapshot_match_below_80pct(tmp_path):
    snaps = tmp_path / "snaps"
    snapshot(snaps, "2026-11-10", names=NAMES[:10])            # only 10 of the contest's 24 players
    src = tmp_path / "src"
    write(src, "555000555", make_csv(users(30)))
    doc = H.build(H.load_dir([src]), snaps)
    assert doc["contests"][0]["snapshot_match"] is None and doc["calibration"]["n"] == 0
    check_schema(doc)


# ── sport filter ─────────────────────────────────────────────────────────────
def test_non_nba_files_are_skipped(tmp_path):
    src = tmp_path / "src"
    write(src, "555001001", make_csv(users(30)))                                         # NBA
    write(src, "555001002", make_csv(users(30), slots=NFL_SLOTS))                        # NFL
    write(src, "555001003", make_csv(users(30), slots=["P", "P", "C", "1B", "2B", "3B", "SS", "OF", "OF", "OF"]))  # MLB
    write(src, "555001004", make_csv(users(30), slots=["G"] * 6))                       # single-slot (PGA-like)
    write(src, "555001005", make_csv(users(30), slots=["CPT", "UTIL", "UTIL", "UTIL", "UTIL", "UTIL"]))  # showdown, unknown
    doc = H.build(H.load_dir([src]), None)
    assert doc["samples"]["contests"] == 1
    sk = {(s["reason"], s["detected"]): s["count"] for s in doc["skipped"]}
    assert sk == {("not_nba", "NFL"): 1, ("not_nba", "MLB"): 1, ("not_nba", "OTHER"): 1, ("unknown_sport", "UNKNOWN"): 1}


def test_showdown_counted_when_names_are_nba(tmp_path):
    src = tmp_path / "src"
    write(src, "555001006", make_csv(users(30), slots=["CPT", "UTIL", "UTIL", "UTIL", "UTIL", "UTIL"]))
    known = {norm_name(n) for n in NAMES}
    doc = H.build(H.load_dir([src]), None, nba_names=known)
    assert doc["samples"]["contests"] == 1 and doc["contests"][0]["kind"] == "showdown"


# ── malformed input never raises (AC-P6) ─────────────────────────────────────
def test_malformed_inputs_never_raise(tmp_path):
    src = tmp_path / "src"
    good = make_csv(users(30))
    write(src, "555002001", "1,2,3\n4,5,6\n")                                  # no header
    write(src, "555002002", ",".join(HEADER) + "\n")                           # header only
    trunc = good.splitlines()
    write(src, "555002003", "\n".join(trunc[:1] + [l[: len(l) // 3] for l in trunc[1:]]) + "\n")  # truncated rows
    write(src, "555002004", good.replace("40.00%", "abc").replace("39.00%", "nan%").replace("38.00%", "inf%"))
    write(src, "555002005", make_csv(users(30), bom=True))                     # BOM
    with zipfile.ZipFile(src / "contest-standings-555002006.zip", "w") as z:   # zip with no CSV
        z.writestr("readme.txt", "nothing here")
    (src / "contest-standings-555002007.zip").write_bytes(b"PK\x03\x04 not really a zip")  # corrupt zip
    (src / "contest-standings-555002008.csv").write_bytes(b"")                 # empty
    (src / "contest-standings-555002009.csv").write_bytes(bytes(range(256)) * 20)  # binary garbage
    write(src, "555002010", good, zip_=True)                                   # good zip
    dup = tmp_path / "src2"
    write(dup, "555002005", make_csv(users(99)))                               # duplicate id in a 2nd folder
    loaded = H.load_dir([src, dup])
    doc = H.build(loaded, None)
    check_schema(doc)
    sk = {s["reason"]: s["count"] for s in doc["skipped"]}
    # no header, corrupt zip, binary -> malformed; header only, empty file, zip w/o csv -> empty
    assert sk.get("malformed", 0) == 3 and sk.get("empty", 0) == 3, doc["skipped"]
    # truncated (lineups cut -> rows still parse with fewer players or are dropped), bad %, BOM, good zip
    assert doc["samples"]["contests"] >= 3
    # duplicate id kept once, from the first folder (30 entries, not 99)
    ids_entries = sorted(c["entries_approx"] for c in doc["contests"])
    assert 99 not in ids_entries
    # %Drafted "abc"/"nan"/"inf" rows ignored: every own value is finite
    assert all(isinstance(r[2], (int, float)) and math.isfinite(r[2]) for r in doc["players"]["rows"])


def test_bom_header_parses_same_as_plain():
    a = C.parse(make_csv(users(20)))
    b = C.parse(make_csv(users(20), bom=True))
    assert a and b and a["own"] == b["own"] and len(a["entries"]) == len(b["entries"]) == 20


def test_build_tolerates_junk_items():
    doc = H.build([None, 3, "x", {"skipped": "malformed"}, {"id": "1", "sport": "NBA", "entries": [], "own": {}}], None)
    check_schema(doc)
    assert doc["samples"]["contests"] == 0
    assert {s["reason"] for s in doc["skipped"]} == {"malformed", "empty"}


def test_empty_input_gives_valid_document(tmp_path):
    doc = H.build(H.load_dir([tmp_path / "does-not-exist"]), tmp_path / "no-snaps")
    check_schema(doc)
    assert doc["contests"] == [] and doc["players"]["rows"] == [] and doc["calibration"]["n"] == 0


# ── privacy (AC-P5) ──────────────────────────────────────────────────────────
def test_privacy_synthetic(tmp_path):
    src = tmp_path / "src"
    us = users(300, tag="zqSecretUser") + ["XyloPhantomZed (1/1)"]
    write(src, "900000123", make_csv(us))
    snaps = tmp_path / "snaps"
    snapshot(snaps, "2026-11-10")
    out = tmp_path / "out" / "contest_history.json"
    H.write_public(H.load_dir([src]), snaps, out)
    text = out.read_text()
    doc = json.loads(text)
    assert not (all_keys(doc) & FORBIDDEN_KEYS)
    for k in FORBIDDEN_KEYS:
        assert f'"{k}"' not in text
    assert "900000123" not in text                 # the source contest id
    assert "zqsecretuser" not in text.lower() and "xylophantomzed" not in text.lower()
    assert "9000000" not in text                     # EntryIds (900000000+i)
    # numeric values never equal a source id
    nums = {v for v in all_values(doc) if isinstance(v, (int, float))}
    assert not nums & {900000123} and not any(900000000 <= v < 900001000 for v in nums)


# ── write_public / CLI (AC-P9) ───────────────────────────────────────────────
def test_write_public_atomic(tmp_path):
    src = tmp_path / "src"
    write(src, "555003001", make_csv(users(30)))
    out = tmp_path / "pub" / "nested" / "contest_history.json"
    doc = H.write_public(H.load_dir([src]), None, out)
    assert json.loads(out.read_text()) == doc
    assert not list(out.parent.glob("*.tmp"))


def test_cli_prints_summary(tmp_path, capsys):
    src = tmp_path / "src"
    write(src, "555004001", make_csv(users(30)))
    write(src, "555004002", make_csv(users(30), slots=NFL_SLOTS))
    out = tmp_path / "cli" / "ch.json"
    assert H.main(["--src", str(src), "--snapshots", str(tmp_path / "none"), "--out", str(out)]) == 0
    line = capsys.readouterr().out
    assert re.search(r"contest_history: 1 NBA contests, 1 skipped, \d+ player rows, 0 calibration pairs", line)
    check_schema(json.loads(out.read_text()))


# ── real DK exports (skipped if absent) ──────────────────────────────────────
real = pytest.mark.skipif(not REAL_DIR.exists(), reason="real DK contest exports not present")


@pytest.fixture(scope="module")
def real_doc(tmp_path_factory):
    if not REAL_DIR.exists():
        pytest.skip("real DK contest exports not present")
    out = tmp_path_factory.mktemp("real") / "contest_history.json"
    snaps = REAL_SNAPS if REAL_SNAPS.exists() else None
    loaded = H.load_dir([REAL_DIR])
    doc = H.write_public(loaded, snaps, out)
    return doc, out.read_text(), loaded


@real
def test_real_counts_and_own_totals(real_doc):
    doc, _, _ = real_doc
    check_schema(doc)
    assert doc["samples"]["contests"] == 8
    assert doc["skipped"] == [{"reason": "not_nba", "detected": "NFL", "count": 4}]
    for c in doc["contests"]:
        if c["kind"] == "classic":
            assert 770 <= c["own_total"] <= 805, (c["key"], c["own_total"])


@real
def test_real_quickley(real_doc):
    doc, _, _ = real_doc
    hits = [r for r in doc["players"]["rows"] if r[1] == "Immanuel Quickley" and r[3] == {"PG": 28.73, "G": 19.73, "UTIL": 4.17}]
    assert len(hits) == 1 and hits[0][2] == pytest.approx(52.63, abs=0.01)
    f = quickley_file(REAL_DIR)
    if f is None:
        pytest.skip("no real export with the Quickley slot rows")
    res = C.parse(f.read_text(encoding="utf-8-sig"))
    assert res["own"][norm_name("Immanuel Quickley")] == pytest.approx(52.63, abs=0.01)


@real
def test_real_privacy(real_doc):
    doc, text, loaded = real_doc
    assert not (all_keys(doc) & FORBIDDEN_KEYS)
    ids = [f.name.split("-")[-1].split(".")[0] for f in REAL_DIR.glob("contest-standings-*")]
    for cid in ids:
        assert cid not in text
    # usernames and entry ids straight from the raw files (not via the parser)
    usernames, entry_ids, player_names = set(), set(), set()
    for f in REAL_DIR.glob("contest-standings-*.csv"):
        with f.open(encoding="utf-8-sig", newline="") as fh:
            r = csv.reader(fh)
            head = next(r)
            ie, iname, ip = head.index("EntryId"), head.index("EntryName"), head.index("Player")
            for row in r:
                if len(row) > iname and row[iname].strip():
                    usernames.add(re.sub(r"\s*\(\d+/\d+\)\s*$", "", row[iname]).strip())
                    entry_ids.add(row[ie].strip())
                if len(row) > ip and row[ip].strip():
                    player_names.add(row[ip].strip().lower())
    pn = " | ".join(player_names)
    cand = {u for u in usernames if len(u) >= 6 and u.lower() not in pn}
    assert len(cand) >= 200
    # match whole string VALUES (not schema keys, not substrings of words): every string leaf
    # of the document, plus whole-word tokens inside those strings
    strings = [v for v in all_values(doc) if isinstance(v, str)]
    lowered = {s.lower() for s in strings}
    tokens = {t for s in strings for t in re.findall(r"[a-z0-9_.\-]+", s.lower())}
    leaked = sorted(u for u in cand if u.lower() in lowered or u.lower() in tokens)
    assert leaked == [], leaked[:10]
    # dict keys that are not schema keys are slot names only (own_by_slot)
    slot_keys = {k for r in doc["players"]["rows"] for k in (r[3] or {})}
    assert slot_keys <= set(NBA_SLOTS) | {"CPT"}
    nums = {str(int(v)) for v in all_values(doc) if isinstance(v, (int, float)) and float(v).is_integer()}
    assert not (nums & entry_ids) and not (nums & set(ids))


# ── Retest 1: R3 (no rewrite when unchanged) and S1 (corrupt first copy) ─────
def _stat(p):
    st = p.stat()
    return p.read_bytes(), st.st_mtime_ns


def test_write_public_skips_unchanged_and_rewrites_changed(tmp_path):
    import os
    import time
    src = tmp_path / "src"
    write(src, "555005001", make_csv(users(40)))
    out = tmp_path / "pub" / "contest_history.json"
    H.write_public(H.load_dir([src]), None, out)
    assert H.write_public.last_written is True
    old = 1_600_000_000_000_000_000                      # pin mtime far in the past so any rewrite shows
    os.utime(out, ns=(old, old))
    before = _stat(out)
    time.sleep(1.1)                                       # generated_at moves to a new second
    H.write_public(H.load_dir([src]), None, out)
    assert H.write_public.last_written is False
    assert _stat(out) == before                           # same bytes, same mtime
    assert not list(out.parent.glob("*.tmp"))
    # changed input -> rewritten
    write(src, "555005002", make_csv(users(60)))
    doc = H.write_public(H.load_dir([src]), None, out)
    assert H.write_public.last_written is True
    after = _stat(out)
    assert after[1] != old and after[0] != before[0]
    assert json.loads(after[0]) == json.loads(json.dumps(doc)) and doc["samples"]["contests"] == 2


def test_write_public_rewrites_corrupt_or_foreign_existing_file(tmp_path):
    src = tmp_path / "src"
    write(src, "555005003", make_csv(users(40)))
    out = tmp_path / "contest_history.json"
    for junk in ("{not json", "[]", "null", json.dumps({"version": 0})):
        out.write_text(junk)
        H.write_public(H.load_dir([src]), None, out)
        assert H.write_public.last_written is True, junk
        check_schema(json.loads(out.read_text()))


def test_cli_reports_unchanged(tmp_path, capsys):
    src = tmp_path / "src"
    write(src, "555005004", make_csv(users(30)))
    out = tmp_path / "ch.json"
    args = ["--src", str(src), "--snapshots", str(tmp_path / "none"), "--out", str(out)]
    H.main(args)
    assert "(written)" in capsys.readouterr().out
    H.main(args)
    assert "unchanged" in capsys.readouterr().out


def test_corrupt_first_copy_does_not_hide_good_duplicate(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    (a / "contest-standings-555006001.zip").write_bytes(b"PK\x03\x04 broken")
    write(b, "555006001", make_csv(users(30)))
    doc = H.build(H.load_dir([a, b]), None)
    assert doc["samples"]["contests"] == 1
    # same folder: corrupt .zip sorts after the good .csv, still one contest
    c = tmp_path / "c"
    write(c, "555006002", make_csv(users(30)))
    (c / "contest-standings-555006002.zip").write_bytes(b"garbage")
    doc2 = H.build(H.load_dir([c]), None)
    assert doc2["samples"]["contests"] == 1 and doc2["skipped"] == []
