"""Analyst (Phase 2): adversarial tests for signed owner runs, the Downloads import
watcher on copies of real DK exports, reminder timing (DST), and the isolation
guard. Everything writes under tmp_path; notify.send / network are always faked."""
import gzip
import json
import logging
import os
import shutil
import stat
import subprocess
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from pipeline import contest_reminders as cr, contests, history, live, notify, owner, owner_setup
from pipeline.config import ET
from tests import conftest

KEY = bytes(range(32))
NOW = 1764200000
CFG = {"server": "https://ntfy.example", "topic": "sigma-own-" + "cd" * 16, "key": KEY}
OLD_TOPIC = "sigma-ctl-99d0abdbacc4ebbb"
ELECTRON = "/Applications/Visual Studio Code.app/Contents/MacOS/Code"
WEB = Path(__file__).resolve().parent.parent / "web"


def msg(slate=156302, req="0123456789abcdef", ts=NOW, key=KEY, sig=None, **extra):
    d = {"v": 1, "action": "run", "slate_id": slate, "req": req, "ts": ts,
         "sig": sig if sig is not None else owner.sign(key, slate, req, ts)}
    d.update(extra)
    return json.dumps(d, ensure_ascii=False)


def V(text, now=NOW, nonces=None, limiter=None, tmp=None):
    nonces = nonces if nonces is not None else owner.NonceStore(tmp / "n.json")
    return owner.verify(text, CFG, now, nonces, limiter if limiter is not None else owner.RateLimiter())[:2]


# ── 1. signing / verification ────────────────────────────────────────────────
def test_wrong_key_and_tampering(tmp_path):
    assert V(msg(), tmp=tmp_path) == (True, "ok")
    assert V(msg(req="1" * 16, key=bytes(32)), tmp=tmp_path) == (False, "bad_sig")         # wrong key
    good = json.loads(msg(req="2" * 16))
    for field, val in (("slate_id", 156303), ("req", "3" * 16), ("ts", NOW + 1)):
        t = dict(good, **{field: val})
        assert V(json.dumps(t), tmp=tmp_path) == (False, "bad_sig"), field
    for bad_sig in ("", good["sig"] + "=", good["sig"].swapcase(), good["sig"][:-1], None, 12345, ["x"]):
        t = dict(good, sig=bad_sig)
        assert V(json.dumps(t), tmp=tmp_path)[0] is False, bad_sig
    t = dict(good)
    t.pop("sig")
    assert V(json.dumps(t), tmp=tmp_path) == (False, "bad_sig")                            # unsigned new format


def test_old_format_and_junk_rejected(tmp_path):
    for text in (json.dumps({"action": "run", "slate_id": 156302, "req": "abc"}), "", "null", "1", "[]",
                 '"run"', "{", "NaN", json.dumps({"v": 1, "action": "run"}), b"\xff\xfe"):
        assert V(text, tmp=tmp_path)[0] is False, text


@pytest.mark.parametrize("ts,ok", [(NOW - 120, True), (NOW + 120, True), (NOW - 121, False), (NOW + 121, False),
                                   (NOW + 10 ** 9, False), (0, False)])
def test_clock_window(ts, ok, tmp_path):
    assert V(msg(ts=ts, req=f"{abs(ts) % 10**16:016x}"), tmp=tmp_path)[0] is ok


def test_size_limit_counts_bytes(tmp_path):
    base = json.loads(msg())
    pad = 2048 - len(msg(pad="").encode())
    exact = msg(pad="x" * pad)
    assert len(exact.encode()) == 2048 and V(exact, tmp=tmp_path) == (True, "ok")
    assert V(msg(req="4" * 16, pad="x" * (pad + 1)), tmp=tmp_path) == (False, "too_big")
    multibyte = msg(req="5" * 16, pad="é" * 1100)                # < 2048 chars, > 2048 bytes
    assert len(multibyte) < 2048 < len(multibyte.encode()) and V(multibyte, tmp=tmp_path) == (False, "too_big")
    assert base["v"] == 1


@pytest.mark.parametrize("slate", [-1, 10 ** 9 + 1, 10 ** 30, 156302.0, "156302", None, True, [156302]])
def test_bad_slate_types(slate, tmp_path):
    good = json.loads(msg())
    good["slate_id"] = slate
    assert V(json.dumps(good), tmp=tmp_path) == (False, "bad_slate")


def test_replay_in_memory_and_after_restart(tmp_path):
    p = tmp_path / "nonces.json"
    m = msg(req="a" * 16)
    assert owner.verify(m, CFG, NOW, owner.NonceStore(p), owner.RateLimiter())[:2] == (True, "ok")
    assert stat.S_IMODE(p.stat().st_mode) == 0o600
    s2 = owner.NonceStore(p)                                       # simulated restart
    assert owner.verify(m, CFG, NOW + 60, s2, owner.RateLimiter())[:2] == (False, "replay")
    assert owner.verify(m, CFG, NOW + 60, s2, owner.RateLimiter())[:2] == (False, "replay")


def test_rate_limit_and_cooldown(tmp_path):
    n, lim = owner.NonceStore(tmp_path / "n.json"), owner.RateLimiter()
    t = NOW
    accepted = 0
    for i in range(14):
        ok, why = owner.verify(msg(req=f"{i:016x}", ts=t), CFG, t, n, lim)[:2]
        if ok:
            accepted += 1
            lim.start(t)
            lim.finish()
        else:
            assert why == "hourly_cap" and i >= 12
        t += 91
    assert accepted == 12
    ok, why = owner.verify(msg(req="e" * 16, ts=t + 3600), CFG, t + 3600, n, lim)[:2]
    assert ok                                                      # window rolled
    lim.start(t + 3600)
    assert owner.verify(msg(req="f" * 16, ts=t + 3601), CFG, t + 3601, n, lim)[:2] == (False, "busy")
    lim.finish()
    assert owner.verify(msg(req="f" * 16, ts=t + 3601), CFG, t + 3610, n, lim)[:2] == (False, "replay")  # busy burned it
    assert owner.verify(msg(req="1f" * 8, ts=t + 3650), CFG, t + 3650, n, lim)[:2] == (False, "cooldown")


def test_every_refusal_burns_the_request_id(tmp_path):
    """R2: a signed request refused for busy, cooldown or hourly_cap is consumed,
    so re-posting it inside its 120 s window gives replay (it used to be accepted)."""
    n, lim = owner.NonceStore(tmp_path / "n.json"), owner.RateLimiter()
    lim.start(NOW)
    lim.finish()
    m = msg(req="9" * 16, ts=NOW + 20)
    assert owner.verify(m, CFG, NOW + 20, n, lim)[:2] == (False, "cooldown")
    assert owner.verify(m, CFG, NOW + 95, n, lim)[:2] == (False, "replay")
    assert owner.verify(m, CFG, NOW + 95, owner.NonceStore(tmp_path / "n.json"), lim)[:2] == (False, "replay")  # after restart
    lim.start(NOW + 200)                                              # busy
    m2 = msg(req="8" * 16, ts=NOW + 210)
    assert owner.verify(m2, CFG, NOW + 210, n, lim)[:2] == (False, "busy")
    lim.finish()
    assert owner.verify(m2, CFG, NOW + 320, n, lim)[:2] == (False, "replay")
    lim2 = owner.RateLimiter()                                        # hourly cap
    lim2.starts = [NOW + 400 + 100 * i for i in range(12)]
    t = NOW + 400 + 1100 + 91
    m3 = msg(req="7" * 16, ts=t)
    assert owner.verify(m3, CFG, t, n, lim2)[:2] == (False, "hourly_cap")
    lim2.starts = []
    assert owner.verify(m3, CFG, t + 10, n, lim2)[:2] == (False, "replay")
    assert owner.verify(msg(req="6" * 16, ts=t + 10), CFG, t + 10, n, lim2)[:2] == (True, "ok")    # a fresh id still works


def _cfg_file(d, mode=0o600, **over):
    doc = {"v": 1, "server": "https://ntfy.sh", "topic": "sigma-own-" + "ef" * 16, "key": owner.b64e(KEY)}
    doc.update(over)
    p = d / owner.CONFIG_NAME
    p.write_text(json.dumps(doc))
    os.chmod(p, mode)
    return p


def test_load_config_fail_closed_matrix(tmp_path):
    assert owner.load_config(tmp_path) is None                               # missing
    _cfg_file(tmp_path)
    assert owner.load_config(tmp_path)["key"] == KEY
    for mode in (0o640, 0o620, 0o604, 0o644, 0o666):
        os.chmod(tmp_path / owner.CONFIG_NAME, mode)
        assert owner.load_config(tmp_path) is None, oct(mode)
    for over in ({"v": 2}, {"server": "http://evil.example"}, {"server": "ftp://x"}, {"topic": "short"},
                 {"topic": "has space in it " * 2}, {"key": owner.b64e(KEY[:31])}, {"key": "!!!"}, {"key": None}):
        _cfg_file(tmp_path, **over)
        assert owner.load_config(tmp_path) is None, over
    (tmp_path / owner.CONFIG_NAME).write_text("{not json")
    os.chmod(tmp_path / owner.CONFIG_NAME, 0o600)
    assert owner.load_config(tmp_path) is None
    d2 = tmp_path / "dir"
    (d2 / owner.CONFIG_NAME).mkdir(parents=True)                             # a directory, not a file
    os.chmod(d2 / owner.CONFIG_NAME, 0o700)
    assert owner.load_config(d2) is None
    d3 = tmp_path / "link"
    d3.mkdir()
    real = tmp_path / "target.json"
    real.write_text(json.dumps({"v": 1, "server": "https://ntfy.sh", "topic": "sigma-own-" + "ef" * 16, "key": owner.b64e(KEY)}))
    os.chmod(real, 0o644)
    os.symlink(real, d3 / owner.CONFIG_NAME)
    assert owner.load_config(d3) is None                                     # symlink to a world-readable file


class _Resp:
    def __init__(self, text):
        self.text = text

    def raise_for_status(self):
        pass


class _Sess:
    def __init__(self, lines=None, exc=None):
        self.lines, self.exc, self.urls = lines or [], exc, []

    def get(self, url, params=None, timeout=None):
        self.urls.append(url)
        if self.exc:
            raise self.exc
        return _Resp("\n".join(json.dumps(x) if not isinstance(x, str) else x for x in self.lines))


def _ev(i, message):
    return {"id": f"e{i}", "event": "message", "message": message}


def test_control_poll_mix_no_secrets_in_logs(monkeypatch, tmp_path, caplog):
    import time
    now = int(time.time())
    good = msg(req="b" * 16, ts=now)
    lines = [_ev(1, json.dumps({"action": "run", "slate_id": 1, "req": "x"})),            # old site
             _ev(2, msg(req="c" * 16, ts=now, key=bytes(32))),                          # wrong key
             _ev(3, msg(req="d" * 16, ts=now - 500)),                                   # stale
             _ev(4, good), _ev(5, good),                                                # valid + replay
             "garbage", {"event": "keepalive"}, ["not", "a", "dict"]]
    sess = _Sess(lines)
    monkeypatch.setattr(live, "session", lambda: sess)
    c = live.Control(CFG, owner.NonceStore(tmp_path / "n.json"), owner.RateLimiter())
    c._logged = 0
    with caplog.at_level(logging.DEBUG):
        out = c.poll()
    assert out == [{"slate_id": 156302, "req": "b" * 16, "ts": now}]
    text = caplog.text + json.dumps([r.args for r in caplog.records], default=str)
    for secret in (CFG["topic"], owner.b64e(KEY), json.loads(good)["sig"], "ntfy.example"):
        assert secret not in text, secret
    assert "bad_format" in caplog.text and "bad_sig" in caplog.text and "stale" in caplog.text and "replay" in caplog.text
    # a failing GET whose error text names the URL is logged by type only
    import requests
    sess2 = _Sess(exc=requests.HTTPError("404 Client Error for url: https://ntfy.example/" + CFG["topic"] + "/json"))
    monkeypatch.setattr(live, "session", lambda: sess2)
    caplog.clear()
    with caplog.at_level(logging.DEBUG):
        assert c.poll() == []
    assert CFG["topic"] not in caplog.text


def test_control_run_releases_limiter_on_error(monkeypatch, tmp_path):
    lim = owner.RateLimiter()
    c = live.Control(CFG, owner.NonceStore(tmp_path / "n.json"), lim)

    def boom(req, do_notify):
        raise RuntimeError("sim crashed")
    monkeypatch.setattr(live, "handle_request", boom)
    with pytest.raises(RuntimeError):
        c.run({"slate_id": 1, "req": "a" * 16, "ts": NOW}, do_notify=False)
    assert lim.in_flight is False and len(lim.starts) == 1


def test_make_control_ignores_old_topic(monkeypatch, caplog):
    monkeypatch.setenv("CONTROL_TOPIC", OLD_TOPIC)
    monkeypatch.setenv("NTFY_SERVER", "https://ntfy.sh")
    with caplog.at_level(logging.INFO):
        assert live.make_control() is None
    assert OLD_TOPIC not in caplog.text
    from pipeline import run
    assert run._control() == {"auth": "hmac-v1"}


def test_setup_code_parses_in_owner_js(tmp_path):
    """owner_setup (temp private dir) -> setup code -> web/src/owner.js parseSetupCode + signRun."""
    if not Path(ELECTRON).exists():
        pytest.skip("no Electron-as-Node runtime")
    import io
    out = io.StringIO()
    assert owner_setup.main(["--private-dir", str(tmp_path / "priv")], out=out) == 0
    code = next(l for l in out.getvalue().splitlines() if l.startswith("sigma-owner-v1:"))
    cfg = owner.load_config(tmp_path / "priv")
    assert cfg is not None
    script = tmp_path / "probe.mjs"
    script.write_text(
        "import { parseSetupCode, signRun } from %s;\n"
        "const r = parseSetupCode(process.argv[2]);\n"
        "if (!r.ok) { console.log(JSON.stringify(r)); process.exit(2); }\n"
        "const sig = await signRun(r.cfg, 156302, '0123456789abcdef', 1764200000);\n"
        "console.log(JSON.stringify({ topic: r.cfg.topic, server: r.cfg.server, sig }));\n"
        % json.dumps((WEB / "src" / "owner.js").as_uri()))
    res = subprocess.run([ELECTRON, str(script), code], capture_output=True, text=True, timeout=60,
                         env={**os.environ, "ELECTRON_RUN_AS_NODE": "1"})
    assert res.returncode == 0, res.stdout + res.stderr
    js = json.loads(res.stdout.strip().splitlines()[-1])
    assert js["topic"] == cfg["topic"] and js["server"] == cfg["server"]
    assert js["sig"] == owner.sign(cfg["key"], 156302, "0123456789abcdef", 1764200000)
    # and what JS signs, the Mac accepts
    m = json.dumps({"v": 1, "action": "run", "slate_id": 156302, "req": "0123456789abcdef", "ts": 1764200000, "sig": js["sig"]})
    assert owner.verify(m, cfg, 1764200000, owner.NonceStore(tmp_path / "n.json"))[:2] == (True, "ok")


# ── 4. import watcher on copies of the real Downloads files ──────────────────
REAL_DL = Path.home() / "Downloads"
REAL_SUB = Path(os.environ.get("SIGMA_TEST_REAL_CONTESTS", str(REAL_DL / "DK DFS Post Contest Results")))


@pytest.fixture
def real_copies(tmp_path, monkeypatch):
    files = sorted(REAL_DL.glob("contest-standings-*")) + sorted(REAL_SUB.glob("contest-standings-*"))
    if not files:
        pytest.skip("no real DK exports")
    dl = tmp_path / "dl"
    dl.mkdir()
    for f in files:
        shutil.copy2(f, dl / f.name)                       # keeps size and (old) mtime
    monkeypatch.setenv("SIGMA_CONTEST_IMPORT", str(dl))
    return dl


def _sport_of(f):
    import csv
    if f.stat().st_size == 0:
        return "EMPTY"
    with f.open(encoding="utf-8-sig", newline="") as fh:
        r = csv.reader(fh)
        h = next(r)
        i = h.index("Roster Position")
        slots = {row[i] for row in r if len(row) > i and row[i]}
    return contests.detect_sport(slots)


def test_real_copies_import_only_nba(real_copies, monkeypatch):
    expect = {contests.FILE_RE.search(f.name).group(1): _sport_of(f) for f in real_copies.iterdir()}
    before = {f.name: (f.stat().st_size, f.stat().st_mtime_ns) for f in real_copies.iterdir()}
    # extra synthetic cases next to the real ones
    nba_src = next(f for f in real_copies.iterdir() if expect[contests.FILE_RE.search(f.name).group(1)] == "NBA")
    with zipfile.ZipFile(real_copies / "contest-standings-900000001.zip", "w") as z:
        z.write(nba_src, "contest-standings-900000001.csv")
    shutil.copy2(nba_src, real_copies / "contest-standings-900000002.csv.crdownload")
    (real_copies / "contest-standings-900000003.csv").write_bytes(b"\x00\x01garbage")
    shutil.copy2(nba_src, real_copies / ("contest-standings-" + contests.FILE_RE.search(nba_src.name).group(1) + " (1).csv"))
    for f in real_copies.iterdir():
        t = 1_700_000_000
        if f.name.startswith("contest-standings-90000"):
            os.utime(f, (t, t))
    new = contests.import_new()
    nba_ids = {k for k, v in expect.items() if v == "NBA"}
    assert set(new) == nba_ids | {"900000001"} and len(new) == len(set(new))          # dedup incl. " (1).csv"
    idx = json.loads((contests.CONTESTS / "index.json").read_text())
    rej = idx["rejected"]
    for cid, sport in expect.items():
        if sport == "EMPTY":
            assert rej[cid]["reason"] == "empty"
        elif sport != "NBA":
            assert rej[cid]["reason"] == "not_nba:" + sport, (cid, rej.get(cid))
    assert rej["900000002"]["reason"] == "partial" and rej["900000003"]["reason"] == "malformed"
    for name, sig in before.items():                                    # originals left in place, unchanged
        f = real_copies / name
        assert f.exists() and (f.stat().st_size, f.stat().st_mtime_ns) == sig
    assert sorted(p.name for p in contests.CONTESTS.glob("*.csv")) == sorted(f"contest-standings-{i}.csv" for i in new)
    calls = []
    real = contests.parse
    monkeypatch.setattr(contests, "parse", lambda t: calls.append(1) or real(t))
    assert contests.import_new() == [] and calls == []                  # cached rejects, nothing re-parsed
    # the public file after refresh: NBA only, no ids or usernames
    contests.refresh()
    doc = json.loads(contests.PUBLIC_OUT.read_text())
    assert doc["samples"]["contests"] == len(nba_ids) + 1   # + the synthetic zip (a copy under a new id)
    assert doc["skipped"] == []                              # nothing non-NBA was imported
    text = contests.PUBLIC_OUT.read_text()
    for cid in expect:
        assert cid not in text


# ── 5. reminders: timing and DST ─────────────────────────────────────────────
class _L(list):
    pass


@pytest.fixture
def sent(tmp_path, monkeypatch):
    out = _L()
    dl = tmp_path / "dl2"
    dl.mkdir()
    monkeypatch.setenv("SIGMA_CONTEST_IMPORT", str(dl))
    monkeypatch.setattr(history, "HISTORY_DIR", tmp_path / "history")
    monkeypatch.setenv("NTFY_TOPIC", "fake-not-real")
    monkeypatch.setattr(notify, "send", lambda *a, **k: out.append((a, k)) or True)

    def no_net(*a, **k):
        raise AssertionError("network call from a reminder test")
    import requests
    monkeypatch.setattr(requests.Session, "request", no_net)
    out.dl = dl
    return out


def _hist(day):
    p = Path(history.HISTORY_DIR) / f"{day}.csv.gz"
    p.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(p, "wt") as f:
        f.write("game,date\n1,%s\n" % day)


@pytest.mark.parametrize("day,utc_before,utc_at", [
    ("2026-03-08", datetime(2026, 3, 9, 12, 59, tzinfo=timezone.utc), datetime(2026, 3, 9, 13, 0, tzinfo=timezone.utc)),   # after spring-forward (EDT)
    ("2026-11-01", datetime(2026, 11, 2, 13, 59, tzinfo=timezone.utc), datetime(2026, 11, 2, 14, 0, tzinfo=timezone.utc)),  # after fall-back (EST)
    ("2026-03-07", datetime(2026, 3, 8, 12, 59, tzinfo=timezone.utc), datetime(2026, 3, 8, 13, 0, tzinfo=timezone.utc)),   # on the DST day itself
])
def test_nine_am_et_across_dst(sent, day, utc_before, utc_at):
    _hist(day)
    assert cr.check(utc_before) == []
    assert cr.check(utc_at) == ["morning:" + day]
    assert cr.check(utc_at + timedelta(hours=5)) == []
    a, k = sent[0]
    assert "mycontests" in (k.get("click") or "") and k.get("markdown") is True


def test_followup_day7_and_window(sent):
    (sent.dl / "draftkings-contest-entry-history.csv").write_text(
        "Sport,Game_Type,Entry_Key,Entry,Contest_Key,Contest_Date_EST,Place\n"
        "NBA,Classic,1,NBA [Main] (x),900100001,2026-11-10 19:00:00,1\n"
        "NBA,Classic,2,Other,900100002,11/12/2026 7:00:00 PM,1\n"
        "nba ,Classic,3,Spaces,900100003,2026-11-12 19:00:00,1\n"
        "MLB,Classic,4,Baseball,900100004,2026-11-10 19:00:00,1\n")
    assert cr.check(datetime(2026, 11, 16, 15, 0, tzinfo=timezone.utc)) == []          # day 6 for c1, nothing ran yesterday
    got = cr.check(datetime(2026, 11, 17, 15, 0, tzinfo=timezone.utc))                 # day 7 for c1
    assert got == ["followup:1"]
    a, k = sent[-1]
    body = a[1]
    assert "exportfullstandingscsv/900100001" in body and "gamecenter/900100001" in body and "900100004" not in body
    assert "\\[Main\\] \\(x\\)" in body                                                # markdown escaped
    assert len(k["actions"]) <= 3
    got = cr.check(datetime(2026, 11, 19, 15, 0, tzinfo=timezone.utc))                 # day 7 for c2/c3
    assert got == ["followup:2"]
    assert cr.check(datetime(2026, 11, 25, 15, 0, tzinfo=timezone.utc)) == []          # > 10 days, already sent


def test_reminder_not_marked_when_send_fails(sent, monkeypatch):
    _hist("2026-11-25")
    monkeypatch.setattr(notify, "send", lambda *a, **k: False)
    assert cr.check(datetime(2026, 11, 26, 15, 0, tzinfo=timezone.utc)) == []
    assert not (Path(contests.PRIVATE) / "reminders.json").exists()
    monkeypatch.setattr(notify, "send", lambda *a, **k: True)
    assert cr.check(datetime(2026, 11, 26, 15, 5, tzinfo=timezone.utc)) == ["morning:2026-11-25"]


def test_notify_send_payload_with_actions_no_network(monkeypatch):
    posted = []

    class S:
        def post(self, url, data=None, json=None, headers=None, timeout=None):
            posted.append({"url": url, "json": json, "data": data, "headers": headers})

            class R:
                status_code = 200

                def raise_for_status(self):
                    pass
            return R()
    import pipeline.notify as N
    monkeypatch.setenv("NTFY_TOPIC", "fake-topic")
    monkeypatch.setattr(N, "session", lambda: S(), raising=False)
    acts = [{"action": "view", "label": str(i), "url": "https://x/%d" % i} for i in range(5)]
    assert N.send("t", "m", actions=acts, markdown=True) is True
    payload = posted[-1]["json"]
    assert posted[-1]["url"] == "https://ntfy.sh" and payload["topic"] == "fake-topic"
    assert len(payload["actions"]) == 3 and payload["markdown"] is True
    N.send("t", "m")
    assert set(posted[-1]["json"]) == {"topic", "title", "message", "priority", "tags"}      # unchanged without extras


# ── 6. isolation guard: every write path, against a decoy root ───────────────
@pytest.fixture
def decoy(tmp_path):
    root = tmp_path / "decoy_root"
    root.mkdir()
    (root / "existing.txt").write_text("x")
    conftest.FORBIDDEN.append(os.path.realpath(str(root)))
    before = len(conftest.VIOLATIONS)
    yield root
    conftest.FORBIDDEN.remove(os.path.realpath(str(root)))
    del conftest.VIOLATIONS[before:]


@pytest.mark.parametrize("name", ["open_w", "open_a", "open_x", "open_rplus", "os_open", "pathlib_write", "pathlib_touch",
                                  "mkdir", "makedirs", "rename_in", "rename_out", "replace", "remove", "unlink", "rmdir",
                                  "rmtree", "chmod", "shutil_copy", "shutil_copy2", "shutil_move", "copytree",
                                  "subprocess_arg", "subprocess_cwd", "truncate"])
def test_guard_blocks(decoy, tmp_path, name):
    src = tmp_path / "src.txt"
    src.write_text("s")
    d = decoy
    actions = {
        "open_w": lambda: open(d / "a", "w"), "open_a": lambda: open(d / "existing.txt", "a"),
        "open_x": lambda: open(d / "b", "x"), "open_rplus": lambda: open(d / "existing.txt", "r+"),
        "os_open": lambda: os.open(str(d / "c"), os.O_WRONLY | os.O_CREAT),
        "pathlib_write": lambda: (d / "e").write_text("x"), "pathlib_touch": lambda: (d / "f").touch(),
        "mkdir": lambda: os.mkdir(d / "g"), "makedirs": lambda: os.makedirs(d / "h" / "i"),
        "rename_in": lambda: os.rename(src, d / "j"), "rename_out": lambda: os.rename(d / "existing.txt", tmp_path / "k"),
        "replace": lambda: os.replace(src, d / "l"), "remove": lambda: os.remove(d / "existing.txt"),
        "unlink": lambda: (d / "existing.txt").unlink(), "rmdir": lambda: os.rmdir(d),
        "rmtree": lambda: shutil.rmtree(d), "chmod": lambda: os.chmod(d / "existing.txt", 0o777),
        "shutil_copy": lambda: shutil.copy(src, d / "m"), "shutil_copy2": lambda: shutil.copy2(src, d / "n"),
        "shutil_move": lambda: shutil.move(str(src), str(d / "o")), "copytree": lambda: shutil.copytree(tmp_path / "decoy_src", d / "p"),
        "subprocess_arg": lambda: subprocess.run(["touch", str(d / "q")], check=False),
        "subprocess_cwd": lambda: subprocess.run(["touch", "r"], cwd=str(d), check=False),
        "truncate": lambda: os.truncate(d / "existing.txt", 0),
    }
    if name == "copytree":
        (tmp_path / "decoy_src").mkdir()
        (tmp_path / "decoy_src" / "f").write_text("1")
    n0 = len(conftest.VIOLATIONS)
    with pytest.raises(PermissionError):
        actions[name]()
    assert len(conftest.VIOLATIONS) > n0
    assert (d / "existing.txt").read_text() == "x"
    assert sorted(p.name for p in d.iterdir()) in (["existing.txt"],), list(d.iterdir())


def test_guard_allows_reads(decoy):
    assert (decoy / "existing.txt").read_text() == "x"
    assert os.listdir(decoy) == ["existing.txt"]
    with open(decoy / "existing.txt", "rb") as f:
        assert f.read() == b"x"
