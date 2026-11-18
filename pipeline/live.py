"""Live loop: `python -m pipeline.live --minutes 30` (started by the Mac updater).

Every pass (about once a minute) re-reads the injury report, starting lineups,
DraftKings and the betting markets, re-projects every player and publishes to
the `data` branch the site reads.

Lineups are built only when you ask: the site's "Run simulations" button sends
a request on your private control channel (an ntfy topic). The loop picks it up
within a few seconds, runs 10,000 lineups x 1,000,000 simulated contests for that
slate with the projections as they stand, publishes the pool, then upgrades it
to exactly-solved lineups. Progress is published in run_status.json.

A watchdog ends any pass that hangs; the updater script restarts the loop.
"""
from __future__ import annotations

import argparse
import faulthandler
import json
import logging
import os
import subprocess
import sys
import time
from datetime import datetime, timezone

from . import contests, patterns, run as runner
from .config import DATA_DIR
from .http import session

log = logging.getLogger("pipeline.live")
PASS_TIMEOUT = 20 * 60   # a pass (incl. a requested simulation) longer than this is treated as hung


def publish() -> bool:
    """Commit the data directory as one rolling commit and push it."""
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}     # never sit waiting for a password

    def git(*args, check=True):
        return subprocess.run(["git", "-C", str(DATA_DIR), *args], check=check,
                              capture_output=True, text=True, env=env, timeout=180)

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
        try:
            r = git("push", "-f", "origin", f"{commit}:refs/heads/data", check=False)
        except subprocess.TimeoutExpired:
            log.warning("push timed out, retrying")
            continue
        if r.returncode == 0:
            git("reset", "-q", "--soft", commit)
            log.info("published %s", commit[:8])
            return True
        log.warning("push failed (%s), retrying", r.stderr.strip()[:200])
        time.sleep(5 * (attempt + 1))
    return False


class Control:
    """Run requests from the site, delivered through an ntfy topic only you and your
    site know. The site posts {"action": "run", "slate_id": ..., "req": ...}."""

    def __init__(self, server: str, topic: str):
        self.url = f"{server.rstrip('/')}/{topic}/json"
        self.since = str(int(time.time()))      # ignore requests sent before we started

    def poll(self) -> list[dict]:
        try:
            r = session().get(self.url, params={"poll": "1", "since": self.since}, timeout=15)
            r.raise_for_status()
        except Exception as e:  # noqa: BLE001
            log.debug("control poll failed: %s", e)
            return []
        out = []
        for line in r.text.splitlines():
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            if ev.get("event") != "message":
                continue
            self.since = ev.get("id") or self.since
            try:
                msg = json.loads(ev.get("message") or "{}")
            except ValueError:
                continue
            if msg.get("action") == "run" and msg.get("slate_id"):
                out.append(msg)
        return out


def write_status(**kw) -> None:
    kw["at"] = datetime.now(timezone.utc).isoformat()
    path = DATA_DIR / "run_status.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(kw, indent=1))


def handle_request(req: dict, do_notify: bool) -> None:
    """Fast pool first (published within about a minute), then the exact upgrade."""
    slate, rid = int(req["slate_id"]), req.get("req", "")
    write_status(state="running", slate_id=slate, req=rid, phase="fast",
                 message="Simulating 10,000 lineups x 1,000,000 contests…")
    publish()
    for phase in ("fast", "exact"):
        res = runner.run(datetime.now(timezone.utc), do_notify=do_notify,
                         sim_request={"slate_id": slate, "phase": phase, "req": rid})
        if res.get("sim_errors"):
            write_status(state="error", slate_id=slate, req=rid, phase=phase, message=res["sim_errors"][0][:300])
            publish()
            return
        if not res.get("sims_ran"):
            write_status(state="error", slate_id=slate, req=rid, phase=phase,
                         message="This slate is locked or no longer listed on DraftKings.")
            publish()
            return
        if phase == "fast":
            write_status(state="running", slate_id=slate, req=rid, phase="exact",
                         message="Lineups ready. Upgrading to exactly-solved lineups (2-3 min)…")
        else:
            write_status(state="done", slate_id=slate, req=rid, phase="exact", message="Exact lineups ready.")
        publish()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--minutes", type=float, default=30.0, help="how long to keep polling")
    ap.add_argument("--interval", type=float, default=60.0, help="seconds between projection passes")
    ap.add_argument("--no-notify", action="store_true")
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    topic = os.environ.get("CONTROL_TOPIC", "").strip()
    control = Control(os.environ.get("NTFY_SERVER", "https://ntfy.sh"), topic) if topic else None
    deadline = time.time() + args.minutes * 60
    last_patterns = 0.0
    status = 0
    first = True
    while time.time() < deadline:
        t0 = time.time()
        # watchdog: if this pass hangs, dump where it is to the log and exit; the
        # updater script restarts the loop a few seconds later
        faulthandler.dump_traceback_later(PASS_TIMEOUT, exit=True)
        try:
            reqs = control.poll() if control else []
            if reqs:
                handle_request(reqs[-1], do_notify=not args.no_notify)
            else:
                res = runner.run(datetime.now(timezone.utc), do_notify=not args.no_notify,
                                 force=args.force and first)
                status = res["status"]
                err_file = DATA_DIR / "updater_error.json"
                if err_file.exists():          # recovered: clear the warning on the site
                    err_file.unlink()
                    res["changed"] = True
                if res["changed"]:
                    publish()
        except Exception as e:  # noqa: BLE001 - keep polling; one bad pass must not end the loop
            log.exception("pass failed: %s", e)
            status = 1
            # show the failure on the site instead of silently going stale
            try:
                (DATA_DIR / "updater_error.json").write_text(json.dumps(
                    {"at": datetime.now(timezone.utc).isoformat(), "error": f"{type(e).__name__}: {e}"[:400]}))
                publish()
            except Exception:  # noqa: BLE001
                pass
        finally:
            faulthandler.cancel_dump_traceback_later()
        first = False
        # historical pattern mining in quiet moments, a few nights at a time
        if time.time() - last_patterns > 600 and deadline - time.time() > 240:
            faulthandler.dump_traceback_later(PASS_TIMEOUT, exit=True)
            try:
                patterns.run(max_new_nights=20)
                publish()
            except Exception as e:  # noqa: BLE001
                log.warning("pattern analysis failed: %s", e)
            finally:
                faulthandler.cancel_dump_traceback_later()
            last_patterns = time.time()
            try:                      # your DraftKings contest exports (private, never published)
                res = contests.refresh()
                if res.get("new"):
                    log.info("imported contests %s; report: %s", res["new"], contests.PRIVATE / "report.html")
            except Exception as e:  # noqa: BLE001
                log.warning("contest import failed: %s", e)
            # once a day: re-check every projection column against real results
            faulthandler.dump_traceback_later(PASS_TIMEOUT, exit=True)
            try:
                if runner.refresh_calibration(datetime.now(timezone.utc)):
                    publish()
            except Exception as e:  # noqa: BLE001
                log.warning("calibration failed: %s", e)
            finally:
                faulthandler.cancel_dump_traceback_later()
        # wait for the next pass, answering "Run simulations" within a few seconds
        while time.time() - t0 < args.interval and time.time() < deadline:
            if control:
                reqs = control.poll()
                if reqs:
                    faulthandler.dump_traceback_later(PASS_TIMEOUT, exit=True)
                    try:
                        handle_request(reqs[-1], do_notify=not args.no_notify)
                    except Exception as e:  # noqa: BLE001
                        log.exception("simulation request failed: %s", e)
                    finally:
                        faulthandler.cancel_dump_traceback_later()
                    break
            time.sleep(4)
    return status


if __name__ == "__main__":
    sys.exit(main())
