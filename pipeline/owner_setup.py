"""Set up owner-only "Run on my Mac" requests.

    python -m pipeline.owner_setup            # first time: create the secret topic + key
    python -m pipeline.owner_setup --show     # print the setup code again
    python -m pipeline.owner_setup --rotate   # new topic + key (every saved browser stops working)

Writes only ~/SigmaDFS-Private/owner_control.json (mode 0600, folder 0700) and
prints a one-line setup code to paste into the site's owner panel (#owner).
No network calls, no other files.
"""
from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import owner

PREFIX = "sigma-owner-v1:"
OLD_TOPIC = "sigma-ctl-99d0abdbacc4ebbb"


def setup_code(server: str, topic: str, key_b64: str) -> str:
    body = json.dumps({"s": server, "t": topic, "k": key_b64}, separators=(",", ":")).encode("utf-8")
    return PREFIX + owner.b64e(body)


def parse_code(code: str) -> dict:
    if not code.startswith(PREFIX):
        raise ValueError("prefix")
    d = json.loads(owner.b64d(code[len(PREFIX):]).decode("utf-8"))
    return {"server": d["s"], "topic": d["t"], "key": d["k"]}


def _write(path: Path, doc: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(doc, f)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    if (os.stat(path).st_mode & 0o077) or (os.stat(path.parent).st_mode & 0o077):
        raise PermissionError("could not restrict permissions")


def _print_code(doc: dict, out) -> None:
    code = setup_code(doc["server"], doc["topic"], doc["key"])
    print("Setup code (paste it into the site's owner panel; keep it private):", file=out)
    print("", file=out)
    print(code, file=out)
    print("", file=out)
    print("Next steps:", file=out)
    print("  1. Open the site and add #owner to the address, paste the code above and press Save.", file=out)
    print("     Do the same on each device you want to run simulations from.", file=out)
    print("  2. Optional tidy-up: delete the CONTROL_TOPIC= line from ~/.sigma-dfs.env. It is ignored now;", file=out)
    print("     run_updater.sh may re-add a random one, which is also ignored and harmless.", file=out)
    print("  3. Restart the updater: launchctl kickstart -k gui/$(id -u)/com.sigmadfs.updater", file=out)
    print(f"  The old topic {OLD_TOPIC} is retired: the Mac no longer listens to it.", file=out)
    print("  To revoke every browser: python -m pipeline.owner_setup --rotate", file=out)


def main(argv=None, out=None) -> int:
    out = out or sys.stdout
    ap = argparse.ArgumentParser(description="Owner-only signed run requests")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--show", action="store_true", help="print the setup code again")
    g.add_argument("--rotate", action="store_true", help="new topic and key (old codes stop working)")
    ap.add_argument("--private-dir", default=None, help="private folder (default: the SigmaDFS-Private folder)")
    ap.add_argument("--server", default=None, help="ntfy server (default NTFY_SERVER or https://ntfy.sh)")
    a = ap.parse_args(argv)
    path = owner.config_path(Path(a.private_dir).expanduser() if a.private_dir else None)
    if a.show:
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            print(f"No owner setup found at {path}. Run without --show first.", file=out)
            return 1
        _print_code(doc, out)
        return 0
    if path.exists() and not a.rotate:
        print(f"Owner setup already exists ({path}). Use --show to print the code or --rotate for a new one.", file=out)
        return 1
    server = (a.server or os.environ.get("NTFY_SERVER") or "https://ntfy.sh").rstrip("/")
    if not owner.valid_server(server):
        print("The ntfy server must start with https://", file=out)
        return 1
    doc = {"v": owner.VERSION, "server": server, "topic": "sigma-own-" + secrets.token_hex(16),
           "key": owner.b64e(secrets.token_bytes(32)), "created_at": datetime.now(timezone.utc).isoformat()}
    try:
        _write(path, doc)
    except OSError as e:
        print(f"Couldn't write {path} with private permissions ({e}). Nothing was set up.", file=out)
        return 1
    print(("Rotated: every browser using the old code stops working." if a.rotate else "Owner setup created.")
          + f" Saved to {path} (mode 0600).", file=out)
    _print_code(doc, out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
