"""Owner-only signed run requests (pipeline/owner.py, owner_setup.py, live.Control)."""
import io
import json
import logging
import os
import stat

import pytest

from pipeline import live, owner, owner_setup, run as runmod

KEY = bytes(range(32))
# shared test vector (P2-2.7); the same string is pinned in web/tests/owner.test.mjs
VECTOR_SIG = "EPWtd6ya9r9DPyRQPy84DjvndYNSYPXOfM8wfzNVpiY"
NOW = 1764200000
CFG = {"server": "https://ntfy.example", "topic": "sigma-own-" + "ab" * 16, "key": KEY}


def _msg(slate=156302, req="0123456789abcdef", ts=NOW, sig=None, **extra):
    d = {"v": 1, "action": "run", "slate_id": slate, "req": req, "ts": ts,
         "sig": sig if sig is not None else owner.sign(KEY, slate, req, ts)}
    d.update(extra)
    return json.dumps(d)


@pytest.fixture
def nonces(tmp_path):
    return owner.NonceStore(tmp_path / "nonces.json")


def test_shared_vector():
    assert owner.canonical(156302, "0123456789abcdef", 1764200000) == "sigma-run-v1\n156302\n0123456789abcdef\n1764200000"
    assert owner.sign(KEY, 156302, "0123456789abcdef", 1764200000) == VECTOR_SIG


def test_accepts_valid(nonces):
    ok, why, req = owner.verify(_msg(), CFG, NOW + 5, nonces, owner.RateLimiter())
    assert ok and why == "ok" and req == {"slate_id": 156302, "req": "0123456789abcdef", "ts": NOW}


@pytest.mark.parametrize("text,reason", [
    ("x" * 2049, "too_big"),
    ("not json", "bad_json"),
    ("[1, 2]", "bad_json"),
    (json.dumps({"action": "run", "slate_id": 156302, "req": "x"}), "bad_format"),     # old unsigned format
    (_msg(v=2), "bad_format"),
    (_msg(action="stop"), "bad_format"),
    (_msg(slate=0), "bad_slate"),
    (_msg(slate="156302"), "bad_slate"),
    (_msg(slate=True), "bad_slate"),
    (_msg(req="0123456789ABCDEF"), "bad_req"),
    (_msg(req="0123"), "bad_req"),
    (_msg(ts=NOW - 121), "stale"),
    (_msg(ts=NOW + 121), "stale"),
    (_msg(ts=float(NOW)), "stale"),
])
def test_rejects(text, reason, nonces):
    assert owner.verify(text, CFG, NOW, nonces, owner.RateLimiter())[:2] == (False, reason)


def test_single_bit_changes_break_signature(nonces):
    good = owner.sign(KEY, 156302, "0123456789abcdef", NOW)
    raw = bytearray(owner.b64d(good))
    raw[0] ^= 1
    assert owner.verify(_msg(sig=owner.b64e(bytes(raw))), CFG, NOW, nonces)[1] == "bad_sig"
    for field, value in (("slate_id", 156303), ("req", "0123456789abcdee"), ("ts", NOW + 1)):
        d = json.loads(_msg(sig=good))
        d[field] = value
        assert owner.verify(json.dumps(d), CFG, NOW, nonces)[1] == "bad_sig", field


def test_replay_blocked_across_restart(tmp_path):
    n1 = owner.NonceStore(tmp_path / "n.json")
    assert owner.verify(_msg(), CFG, NOW, n1)[0]
    assert owner.verify(_msg(), CFG, NOW + 1, n1)[1] == "replay"
    n2 = owner.NonceStore(tmp_path / "n.json")          # a restarted Mac reloads the persisted ids
    assert owner.verify(_msg(), CFG, NOW + 2, n2)[1] == "replay"
    assert stat.S_IMODE(os.stat(tmp_path / "n.json").st_mode) == 0o600


def test_rate_limits(nonces):
    lim = owner.RateLimiter()
    t = NOW
    reqs = iter(f"{i:016x}" for i in range(100))

    def attempt(at):
        r = next(reqs)
        return owner.verify(_msg(req=r, ts=at), CFG, at, nonces, lim)
    assert attempt(t)[0]
    lim.start(t)
    assert attempt(t + 5)[1] == "busy"
    lim.finish()
    assert attempt(t + 60)[1] == "cooldown"            # < 90 s since the last accepted run
    for i in range(1, 12):                               # 12 accepted runs in the hour
        at = t + i * 91
        assert attempt(at)[0], i
        lim.start(at)
        lim.finish()
    assert attempt(t + 12 * 91)[1] == "hourly_cap"     # the 13th within 60 min
    assert attempt(t + 3600 + 91)[0]                     # the window rolls on


def test_compare_digest_used():
    src = open(owner.__file__).read()
    assert "hmac.compare_digest" in src and "sig ==" not in src and "== sig" not in src


def _setup(tmp_path, *args):
    out = io.StringIO()
    code = owner_setup.main(["--private-dir", str(tmp_path / "priv"), *args], out=out)
    return code, out.getvalue()


def test_owner_setup_cli(tmp_path, monkeypatch):
    from pipeline import http

    def no_network(*a, **k):
        raise AssertionError("network used")
    monkeypatch.setattr(http, "session", no_network)
    code, out = _setup(tmp_path)
    assert code == 0
    path = tmp_path / "priv" / "owner_control.json"
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600 and stat.S_IMODE(os.stat(path.parent).st_mode) == 0o700
    assert sorted(p.name for p in path.parent.iterdir()) == ["owner_control.json"]       # nothing else written
    doc = json.loads(path.read_text())
    line = next(l for l in out.splitlines() if l.startswith("sigma-owner-v1:"))
    parsed = owner_setup.parse_code(line)
    assert parsed == {"server": doc["server"], "topic": doc["topic"], "key": doc["key"]}
    assert __import__("re").match(r"^sigma-own-[0-9a-f]{32}$", doc["topic"]) and len(owner.b64d(doc["key"])) == 32
    assert owner.load_config(tmp_path / "priv")["topic"] == doc["topic"]
    assert _setup(tmp_path)[0] == 1                                  # refuses without --rotate
    code, out2 = _setup(tmp_path, "--show")
    assert code == 0 and line in out2
    assert _setup(tmp_path, "--rotate")[0] == 0
    doc2 = json.loads(path.read_text())
    assert doc2["topic"] != doc["topic"] and doc2["key"] != doc["key"]


def test_load_config_fails_closed(tmp_path):
    priv = tmp_path / "p"
    assert owner.load_config(priv) is None                            # missing
    priv.mkdir()
    f = priv / "owner_control.json"
    f.write_text("{not json")
    os.chmod(f, 0o600)
    assert owner.load_config(priv) is None                            # malformed
    good = {"v": 1, "server": "https://ntfy.sh", "topic": "sigma-own-" + "0" * 32, "key": owner.b64e(KEY)}
    f.write_text(json.dumps(dict(good, key=owner.b64e(KEY[:16]))))
    assert owner.load_config(priv) is None                            # short key
    f.write_text(json.dumps(good))
    os.chmod(f, 0o644)
    assert owner.load_config(priv) is None                            # group/other-readable
    os.chmod(f, 0o600)
    assert owner.load_config(priv)["key"] == KEY


class _Resp:
    def __init__(self, text):
        self.text = text

    def raise_for_status(self):
        pass


class _Sess:
    def __init__(self, text):
        self.text, self.calls = text, []

    def get(self, url, params=None, timeout=None):
        self.calls.append(url)
        return _Resp(self.text)


def _feed(*messages):
    return "\n".join(json.dumps({"id": f"m{i}", "event": "message", "message": m}) for i, m in enumerate(messages))


def test_control_poll_filters_and_logs_no_secrets(monkeypatch, tmp_path, caplog):
    import time
    now = int(time.time())
    good = _msg(req="00000000000000aa", ts=now)
    feed = _feed(
        json.dumps({"action": "run", "slate_id": 156302, "req": "x"}),             # old unsigned
        good,
        good,                                                                         # replay
        _msg(req="00000000000000bb", ts=now - 500),                                   # stale
        _msg(req="00000000000000cc", ts=now, sig="A" * 43),                           # forged
    )
    monkeypatch.setattr(live, "session", lambda: _Sess(feed))
    c = live.Control(CFG, nonces=owner.NonceStore(tmp_path / "n.json"))
    c.LOG_EVERY = 0
    with caplog.at_level(logging.DEBUG):
        reqs = c.poll()
    assert reqs == [{"slate_id": 156302, "req": "00000000000000aa", "ts": now}]
    text = " ".join(r.getMessage() for r in caplog.records)
    assert "ignored 4" in text
    for secret in (CFG["topic"], owner.b64e(KEY), json.loads(good)["sig"]):
        assert secret not in text


def test_main_fail_closed_without_config(monkeypatch, tmp_path, caplog):
    """No owner_control.json: even with the burned CONTROL_TOPIC set, nothing is polled or run."""
    monkeypatch.setenv("CONTROL_TOPIC", "sigma-ctl-99d0abdbacc4ebbb")
    handled, polled = [], []
    monkeypatch.setattr(live, "handle_request", lambda *a, **k: handled.append(a))
    monkeypatch.setattr(live, "session", lambda: polled.append(1) or _Sess(_feed(
        json.dumps({"action": "run", "slate_id": 156302, "req": "x"}))))
    with caplog.at_level(logging.INFO):
        assert live.make_control() is None
    assert "CONTROL_TOPIC is ignored" in caplog.text
    monkeypatch.setattr(live.runner, "run", lambda *a, **k: {"status": 0, "changed": False})
    monkeypatch.setattr(live.patterns, "run", lambda **k: None)
    monkeypatch.setattr(live, "publish", lambda: False)
    monkeypatch.setattr(live.contests, "watch", lambda *a, **k: [])
    monkeypatch.setattr(live.contests, "refresh", lambda: {})
    monkeypatch.setattr(live.contest_reminders, "check", lambda *a, **k: [])
    monkeypatch.setattr(live.runner, "snapshot_industry", lambda *a: None)
    monkeypatch.setattr(live.runner, "refresh_benchmark", lambda *a: None)
    monkeypatch.setattr(live.runner, "refresh_calibration", lambda *a: False)
    monkeypatch.setattr(live.NewsWatch, "changed", lambda self: True)
    live.main(["--minutes", "0.001", "--interval", "0", "--no-notify"])
    assert handled == [] and polled == []


def test_main_with_config_runs_signed(monkeypatch, tmp_path):
    from pipeline import contests
    priv = contests.PRIVATE
    priv.mkdir(parents=True, exist_ok=True)
    f = priv / "owner_control.json"
    f.write_text(json.dumps({"v": 1, "server": CFG["server"], "topic": CFG["topic"], "key": owner.b64e(KEY)}))
    os.chmod(f, 0o600)
    c = live.make_control()
    assert c is not None and c.url == f"{CFG['server']}/{CFG['topic']}/json"
    seen = []
    monkeypatch.setattr(live, "handle_request", lambda req, do_notify: seen.append((req, c.limiter.in_flight)))
    c.run({"slate_id": 1, "req": "r"}, do_notify=False)
    assert seen == [({"slate_id": 1, "req": "r"}, True)] and not c.limiter.in_flight


def test_meta_control_has_no_topic(monkeypatch):
    monkeypatch.setenv("CONTROL_TOPIC", "sigma-ctl-99d0abdbacc4ebbb")
    monkeypatch.setenv("NTFY_SERVER", "https://ntfy.sh")
    assert runmod._control() == {"auth": "hmac-v1"}


def test_refused_request_cannot_be_replayed_later(nonces):
    """R2: a signed request refused by the rate limit is still used up."""
    lim = owner.RateLimiter()
    assert owner.verify(_msg(req="1111111111111111", ts=NOW), CFG, NOW, nonces, lim)[0]
    lim.start(NOW)
    lim.finish()
    refused = _msg(req="2222222222222222", ts=NOW + 30)
    assert owner.verify(refused, CFG, NOW + 30, nonces, lim)[1] == "cooldown"
    assert owner.verify(refused, CFG, NOW + 95, nonces, lim)[1] == "replay"       # re-posted after the cooldown
    lim.start(NOW + 30)
    busy = _msg(req="3333333333333333", ts=NOW + 200)
    assert owner.verify(busy, CFG, NOW + 200, nonces, lim)[1] == "busy"
    lim.finish()
    assert owner.verify(busy, CFG, NOW + 210, nonces, lim)[1] == "replay"


def test_patterns_reject_trailing_newline(nonces):
    assert owner.verify(_msg(req="0123456789abcdef\n"), CFG, NOW, nonces)[1] == "bad_req"
    assert not owner.TOPIC_RE.fullmatch("sigma-own-" + "ab" * 16 + "\n")
