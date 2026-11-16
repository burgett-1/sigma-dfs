"""Near-real-time loop: `python -m pipeline.live --minutes 14`.

GitHub starts this every 15 minutes. Inside each run it polls every ~60 seconds
(injury report, NBA.com starting lineups, DraftKings, odds), so news is picked
up within about a minute instead of waiting for the next scheduled run. When
anything changes, projections and the 10,000-lineup simulation are rebuilt,
notifications go out, and the results are pushed to the `data` branch, which
the app reads directly (no website redeploy needed).

In quiet periods it also works through the historical winning-lineup analysis
a few nights at a time.
"""
from __future__ import annotations

import argparse
import logging
import subprocess
import sys
import time
from datetime import datetime, timezone

from . import patterns, run as runner
from .config import DATA_DIR

log = logging.getLogger("pipeline.live")


def publish() -> bool:
    """Commit the data directory as one rolling commit and push it."""
    def git(*args, check=True):
        return subprocess.run(["git", "-C", str(DATA_DIR), *args], check=check,
                              capture_output=True, text=True)

    if not (DATA_DIR / ".git").exists():
        log.info("data directory is not a git worktree; skipping publish")
        return False
    git("add", "-A")
    has_head = git("rev-parse", "-q", "--verify", "HEAD", check=False).returncode == 0
    if has_head and git("diff", "--cached", "--quiet", check=False).returncode == 0:
        return False
    tree = git("write-tree").stdout.strip()
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%MZ")
    commit = git("commit-tree", tree, "-m", f"data {stamp}").stdout.strip()
    for attempt in range(4):
        r = git("push", "-f", "origin", f"{commit}:refs/heads/data", check=False)
        if r.returncode == 0:
            git("reset", "-q", "--soft", commit)
            log.info("published %s", commit[:8])
            return True
        log.warning("push failed (%s), retrying", r.stderr.strip()[:200])
        time.sleep(5 * (attempt + 1))
    return False


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--minutes", type=float, default=14.0, help="how long to keep polling")
    ap.add_argument("--interval", type=float, default=60.0, help="seconds between polls")
    ap.add_argument("--no-notify", action="store_true")
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    deadline = time.time() + args.minutes * 60
    last_patterns = 0.0
    status = 0
    first = True
    while True:
        t0 = time.time()
        try:
            res = runner.run(datetime.now(timezone.utc), do_notify=not args.no_notify,
                             force=args.force and first)
            status = res["status"]
            if res["changed"]:
                publish()
        except Exception as e:  # noqa: BLE001 - keep polling; one bad pass must not end the loop
            log.exception("pass failed: %s", e)
            status = 1
        first = False
        # historical pattern mining in quiet moments, a few nights at a time
        if time.time() - last_patterns > 600 and deadline - time.time() > 240:
            try:
                patterns.run(max_new_nights=20)
                publish()
            except Exception as e:  # noqa: BLE001
                log.warning("pattern analysis failed: %s", e)
            last_patterns = time.time()
        if time.time() + args.interval > deadline:
            break
        time.sleep(max(0.0, args.interval - (time.time() - t0)))
    return status


if __name__ == "__main__":
    sys.exit(main())
