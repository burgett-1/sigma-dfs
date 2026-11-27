"""Test isolation: no test may touch the real private folder, the live clone or
any real notification channel.

1. Before any `pipeline` import, the data/private/import folders are pointed at a
   fresh temp root and the secret env vars are removed (a shell that sourced
   ~/.sigma-dfs.env can't reach ntfy, X or the Odds API from a test).
2. An autouse fixture re-points every module-level path constant at tmp_path.
3. An audit hook refuses (and records) any write, rename, delete, chmod or
   subprocess naming ~/SigmaDFS-Private or ~/sigma-dfs. The session fails if
   anything was recorded, even when a test swallowed the exception.
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

# ── 1. environment, before anything imports pipeline ───────────────────────────
TEST_ROOT = Path(tempfile.mkdtemp(prefix="sigma-test-"))
for _name, _sub in (("SIGMA_PRIVATE_DIR", "private"), ("SIGMA_DATA_DIR", "data"), ("SIGMA_CONTEST_IMPORT", "import"),
                    ("SIGMA_CACHE_DIR", "cache")):
    (TEST_ROOT / _sub).mkdir(parents=True, exist_ok=True)
    os.environ[_name] = str(TEST_ROOT / _sub)
SECRET_ENV = ("CONTROL_TOPIC", "NTFY_TOPIC", "NTFY_TOKEN", "NTFY_SERVER", "X_BEARER_TOKEN", "ODDS_API_KEY",
              "SIGMA_DK_USERNAME")
for _name in SECRET_ENV:
    os.environ.pop(_name, None)
# other sports stay off unless a test turns them on (no scheduler, no NFL network in NBA tests)
os.environ["SIGMA_SPORTS"] = "NBA"
# read-only real sample folders used by skip-if-absent tests (overridable)
os.environ.setdefault("SIGMA_TEST_REAL_CONTESTS", str(Path.home() / "Downloads" / "DK DFS Post Contest Results"))
os.environ.setdefault("SIGMA_TEST_REAL_SNAPSHOTS", str(Path.home() / "SigmaDFS-Private" / "projections"))

# ── 3. write guard ─────────────────────────────────────────────────────────────
FORBIDDEN = [os.path.realpath(str(Path.home() / "SigmaDFS-Private")), os.path.realpath(str(Path.home() / "sigma-dfs")),
             os.path.realpath(str(Path.home() / ".cache" / "sigma-dfs"))]
_REPO = os.path.realpath(str(Path(__file__).resolve().parent.parent))
if _REPO == FORBIDDEN[1] or _REPO.startswith(FORBIDDEN[1] + os.sep):
    raise SystemExit("run tests from the dev checkout, not the live clone (~/sigma-dfs)")
VIOLATIONS: list[str] = []


def _home_alias(root: str) -> str:
    """'~/SigmaDFS-Private' form of a root, as it may appear in a shell command."""
    home = os.path.realpath(str(Path.home()))
    return "~" + root[len(home):] if root.startswith(home) else root
_GUARD = {"on": True}
_WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_TRUNC


def _forbidden(p) -> str | None:
    try:
        if isinstance(p, int):
            return None
        s = os.path.realpath(os.fsdecode(p))        # resolves symlinked parents too
    except Exception:  # noqa: BLE001
        return None
    for root in FORBIDDEN:
        if s == root or s.startswith(root + os.sep):
            return s
    return None


def _hook(event, args):
    if not _GUARD["on"]:
        return
    hit = None
    if event == "open":
        path, mode, flags = (list(args) + [None, None, None])[:3]
        writing = (isinstance(mode, str) and any(c in mode for c in "wax+")) or \
                  (isinstance(flags, int) and flags & _WRITE_FLAGS)
        if writing:
            hit = _forbidden(path)
    elif event in ("os.mkdir", "os.remove", "os.rmdir", "os.chmod", "shutil.rmtree", "os.truncate", "os.utime"):
        hit = _forbidden(args[0]) if args else None
    elif event in ("os.rename", "os.replace"):              # either end of a rename counts
        hit = _forbidden(args[0]) or (_forbidden(args[1]) if len(args) > 1 else None)
    elif event in ("shutil.move", "shutil.copyfile", "shutil.copytree", "os.symlink", "os.link"):   # destination
        hit = _forbidden(args[1]) if len(args) > 1 else None
    elif event == "os.system":                              # scan the shell command string
        cmd = args[0] if args else ""
        try:
            cmd = os.fsdecode(cmd)
        except Exception:  # noqa: BLE001
            cmd = str(cmd)
        hit = next((cmd for root in FORBIDDEN if root in cmd or _home_alias(root) in cmd), None)
    elif event in ("os.posix_spawn", "os.posix_spawnp", "os.exec", "os.spawn"):   # path + argv
        parts = []
        for a in args[:2]:
            parts.extend(a if isinstance(a, (list, tuple)) else [a])
        for a in parts:
            try:
                txt = os.fsdecode(a)
            except Exception:  # noqa: BLE001
                continue
            if _forbidden(txt) or any(root in txt for root in FORBIDDEN):
                hit = txt
                break
    elif event == "subprocess.Popen":
        parts = args[1] if len(args) > 1 else None
        parts = [parts] if isinstance(parts, (str, bytes, os.PathLike)) else list(parts or [])
        for a in parts + [args[2] if len(args) > 2 else None]:
            if a is None:
                continue
            try:
                s = os.fsdecode(a) if not isinstance(a, str) else a
            except Exception:  # noqa: BLE001
                continue
            if any(root in s for root in FORBIDDEN):
                hit = s
                break
    if hit:
        VIOLATIONS.append(f"{event}: {hit}")
        raise PermissionError(f"test tried to write {hit}")


sys.addaudithook(_hook)

import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _isolate_paths(tmp_path, monkeypatch):
    """Point every module-level path constant at tmp_path (per-test patches still win)."""
    data, priv = tmp_path / "_data", tmp_path / "_private"
    for d in (data, priv, tmp_path / "_import", tmp_path / "_cache"):
        d.mkdir(exist_ok=True)
    monkeypatch.setenv("SIGMA_CONTEST_IMPORT", str(tmp_path / "_import"))
    monkeypatch.setenv("SIGMA_SPORTS", "NBA")
    from pipeline import benchmark, calibration, config, contest_history, contests, history, live, markets, news, \
        patterns, run
    for mod in (config, run, patterns, markets, contest_history, contests, live, news):
        monkeypatch.setattr(mod, "DATA_DIR", data, raising=False)
    for mod in (config, run, patterns):
        monkeypatch.setattr(mod, "STATE_DIR", data / "state", raising=False)
        monkeypatch.setattr(mod, "SLATES_DIR", data / "slates", raising=False)
    for mod in (config, history):
        monkeypatch.setattr(mod, "HISTORY_DIR", data / "history", raising=False)
    monkeypatch.setattr(calibration, "PATH", data / "state" / "calibration.json", raising=False)
    monkeypatch.setattr(patterns, "NIGHTS", data / "patterns" / "nights", raising=False)
    monkeypatch.setattr(patterns, "OUT", data / "patterns.json", raising=False)
    monkeypatch.setattr(contests, "PRIVATE", priv)
    monkeypatch.setattr(contests, "CONTESTS", priv / "contests")
    monkeypatch.setattr(contests, "SNAPSHOTS", priv / "projections")
    monkeypatch.setattr(contests, "OWN_CAL", priv / "own_calibration.json")
    monkeypatch.setattr(contests, "PUBLIC_OUT", data / "contest_history.json")
    monkeypatch.setattr(contests, "_WATCH", {"at": 0.0}, raising=False)
    monkeypatch.setattr(benchmark, "DIR", priv / "benchmark", raising=False)
    monkeypatch.setattr(benchmark, "REPORT", priv / "benchmark" / "accuracy.json", raising=False)
    monkeypatch.setattr(news, "NEWS_DIR", priv / "news", raising=False)
    monkeypatch.setattr(news, "ITEMS", priv / "news" / "x_items.json", raising=False)
    monkeypatch.setattr(news, "SPEED", priv / "news" / "speed.json", raising=False)
    monkeypatch.setattr(config, "CACHE_DIR", tmp_path / "_cache", raising=False)
    for name in SECRET_ENV:
        monkeypatch.delenv(name, raising=False)
    yield


def pytest_sessionfinish(session, exitstatus):
    if VIOLATIONS:
        print("\n\nTEST ISOLATION VIOLATIONS (writes to the real private folder or live clone):")
        for v in VIOLATIONS:
            print("  " + v)
        session.exitstatus = 1
