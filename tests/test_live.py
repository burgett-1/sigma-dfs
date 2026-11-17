import json

from pipeline import live


class _Resp:
    def __init__(self, text):
        self.text = text

    def raise_for_status(self):
        pass


class _Sess:
    def __init__(self, text):
        self.text, self.calls = text, []

    def get(self, url, params=None, timeout=None):
        self.calls.append((url, dict(params or {})))
        return _Resp(self.text)


def test_control_poll_parses_run_requests(monkeypatch):
    lines = [
        {"id": "a1", "event": "open"},
        {"id": "a2", "event": "message", "message": json.dumps({"action": "run", "slate_id": 156751, "req": "x"})},
        {"id": "a3", "event": "message", "message": "not json"},
        {"id": "a4", "event": "message", "message": json.dumps({"action": "other"})},
    ]
    sess = _Sess("\n".join(json.dumps(l) for l in lines))
    monkeypatch.setattr(live, "session", lambda: sess)
    c = live.Control("https://ntfy.sh", "sigma-ctl-test")
    reqs = c.poll()
    assert reqs == [{"action": "run", "slate_id": 156751, "req": "x"}]
    assert c.since == "a4"                         # next poll continues after the last message
    assert sess.calls[0][0] == "https://ntfy.sh/sigma-ctl-test/json" and sess.calls[0][1]["poll"] == "1"


def test_handle_request_runs_fast_then_exact(tmp_path, monkeypatch):
    monkeypatch.setattr(live, "DATA_DIR", tmp_path)
    calls, statuses = [], []
    monkeypatch.setattr(live, "publish", lambda: statuses.append(json.loads((tmp_path / "run_status.json").read_text())))

    def fake_run(now, do_notify=True, force=False, sim_request=None):
        calls.append(sim_request["phase"])
        return {"status": 0, "changed": True, "sims_ran": [(sim_request["slate_id"], sim_request["phase"])], "sim_errors": []}
    monkeypatch.setattr(live.runner, "run", fake_run)
    live.handle_request({"slate_id": 555, "req": "r9"}, do_notify=False)
    assert calls == ["fast", "exact"]
    assert [s["state"] for s in statuses] == ["running", "running", "done"]
    assert statuses[1]["phase"] == "exact" and all(s["req"] == "r9" for s in statuses)


def test_handle_request_reports_locked_slate(tmp_path, monkeypatch):
    monkeypatch.setattr(live, "DATA_DIR", tmp_path)
    statuses = []
    monkeypatch.setattr(live, "publish", lambda: statuses.append(json.loads((tmp_path / "run_status.json").read_text())))
    monkeypatch.setattr(live.runner, "run", lambda *a, **k: {"status": 0, "changed": False, "sims_ran": [], "sim_errors": []})
    live.handle_request({"slate_id": 1, "req": "r"}, do_notify=False)
    assert statuses[-1]["state"] == "error" and "locked" in statuses[-1]["message"]
