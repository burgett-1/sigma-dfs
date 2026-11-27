"""Self-tests for the conftest isolation and write guard (BUG-7 regression)."""
import os
from pathlib import Path

import pytest

from tests import conftest


def test_guard_blocks_and_records_private_write():
    probe = Path.home() / "SigmaDFS-Private" / "__guard_probe__"
    before = len(conftest.VIOLATIONS)
    with pytest.raises(PermissionError):
        open(probe, "w")
    with pytest.raises(PermissionError):
        os.replace(str(Path(conftest.TEST_ROOT) / "nope"), str(probe))
    assert len(conftest.VIOLATIONS) >= before + 2
    assert not probe.exists()
    del conftest.VIOLATIONS[before:]          # deliberate probes: keep the session green


def test_guard_blocks_live_clone_write():
    before = len(conftest.VIOLATIONS)
    with pytest.raises(PermissionError):
        (Path.home() / "sigma-dfs" / "__guard_probe__").write_text("x")
    del conftest.VIOLATIONS[before:]


def test_reads_are_allowed():
    # reading (or merely checking) real folders is fine
    (Path.home() / "SigmaDFS-Private").exists()


def test_paths_point_at_temp(tmp_path):
    from pipeline import benchmark, config, contests, news, run
    assert str(contests.PRIVATE).startswith(str(tmp_path))
    assert str(contests.SNAPSHOTS).startswith(str(tmp_path))
    assert str(run.DATA_DIR).startswith(str(tmp_path)) and str(config.DATA_DIR).startswith(str(tmp_path))
    assert str(benchmark.DIR).startswith(str(tmp_path)) and str(news.ITEMS).startswith(str(tmp_path))
    # import-time defaults were redirected too (env set before any pipeline import)
    assert os.environ["SIGMA_PRIVATE_DIR"].startswith(str(conftest.TEST_ROOT))


def test_secret_env_absent():
    for name in conftest.SECRET_ENV:
        assert name not in os.environ, name


def test_control_publishes_no_topic():
    from pipeline import run
    assert run._control() == {"auth": "hmac-v1"}


def test_run_snapshot_lands_in_tmp(tmp_path):
    # BUG-7: run.py's contests.snapshot() used to write the real ~/SigmaDFS-Private/projections
    from pipeline import contests
    contests.snapshot(555, "2026-11-15", [{"name": "Bos Player A", "proj": 30.0, "own": 10.0, "salary": 5000}])
    assert (tmp_path / "_private" / "projections" / "2026-11-15" / "555.json").exists()
    assert not (Path.home() / "SigmaDFS-Private" / "projections" / "2026-11-15" / "555.json").exists()


# ── Refinement 1 (R3): more write paths, all probed against a decoy root ─────────
@pytest.fixture
def decoy(tmp_path, monkeypatch):
    root = tmp_path / "decoy"
    root.mkdir()
    (root / "existing").write_text("x")              # written before the decoy is guarded
    outside = tmp_path / "outside.txt"
    outside.write_text("o")
    real = os.path.realpath(str(root))
    monkeypatch.setattr(conftest, "FORBIDDEN", [real])
    before = len(conftest.VIOLATIONS)
    yield root, outside
    assert sorted(p.name for p in root.iterdir()) == ["existing"]     # nothing was created
    assert len(conftest.VIOLATIONS) > before
    del conftest.VIOLATIONS[before:]


def test_guard_resolves_symlinked_parent(decoy, tmp_path):
    root, _ = decoy
    link = tmp_path / "alias"
    os.symlink(root, link)                           # creating the link outside the root is fine
    with pytest.raises(PermissionError):
        open(link / "via_link", "w")


def test_guard_blocks_os_system(decoy):
    root, _ = decoy
    with pytest.raises(PermissionError):
        os.system(f"touch {root}/sys")


def test_guard_blocks_posix_spawn(decoy):
    root, _ = decoy
    with pytest.raises(PermissionError):
        os.posix_spawn("/usr/bin/touch", ["touch", f"{root}/spawned"], dict(os.environ))


def test_guard_blocks_symlink_destination(decoy):
    root, outside = decoy
    with pytest.raises(PermissionError):
        os.symlink(outside, root / "sym")


def test_guard_blocks_hard_link_destination(decoy):
    root, outside = decoy
    with pytest.raises(PermissionError):
        os.link(outside, root / "hard")


def test_guard_blocks_utime(decoy):
    root, _ = decoy
    m = (root / "existing").stat().st_mtime
    with pytest.raises(PermissionError):
        os.utime(root / "existing", (0, 0))
    assert (root / "existing").stat().st_mtime == m
