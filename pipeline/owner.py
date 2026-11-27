"""Owner-only remote runs: signed "Run on my Mac" requests (hmac-v1).

The site's owner panel holds a setup code (server, secret topic, 32-byte key)
printed by `python -m pipeline.owner_setup`. It signs each request; the Mac
accepts only correctly signed, fresh, never-seen requests and rate-limits them.
Nothing here is ever published: the config lives in the private folder
(~/SigmaDFS-Private/owner_control.json, mode 0600).

Signed string: "sigma-run-v1\\n<slate_id>\\n<req>\\n<ts>", HMAC-SHA256, base64url
without padding. Logs carry reason codes only, never the message, key or topic.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import re
import stat
import time
from pathlib import Path

from . import contests

log = logging.getLogger(__name__)

VERSION = 1
CONFIG_NAME = "owner_control.json"
NONCE_NAME = "control_nonces.json"
MAX_BYTES = 2048
MAX_SKEW = 120            # seconds either side of the Mac's clock
NONCE_TTL = 15 * 60
COOLDOWN = 90             # seconds between accepted runs
PER_HOUR = 12             # accepted runs in any rolling hour
TOPIC_RE = re.compile(r"[A-Za-z0-9_-]{20,64}")      # used with fullmatch (no trailing-newline slack)
REQ_RE = re.compile(r"[0-9a-f]{16}")
REASONS = ("too_big", "bad_json", "bad_format", "bad_slate", "bad_req", "stale", "bad_sig", "replay",
           "busy", "cooldown", "hourly_cap")


def private_dir(private: Path | None = None) -> Path:
    """Resolved at call time so tests (and --private-dir) redirect it."""
    return Path(private) if private is not None else Path(contests.PRIVATE)


def config_path(private: Path | None = None) -> Path:
    return private_dir(private) / CONFIG_NAME


def b64e(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")


def b64d(s: str) -> bytes:
    s = str(s).strip()
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def canonical(slate_id: int, req: str, ts: int) -> str:
    return f"sigma-run-v1\n{slate_id}\n{req}\n{ts}"


def sign(key: bytes, slate_id: int, req: str, ts: int) -> str:
    return b64e(hmac.new(key, canonical(slate_id, req, ts).encode("utf-8"), hashlib.sha256).digest())


def valid_server(s) -> bool:
    return isinstance(s, str) and (s.startswith("https://") or s.startswith("http://localhost")) and len(s) < 200


def load_config(private: Path | None = None) -> dict | None:
    """{"server", "topic", "key": bytes} or None (fail closed: missing, malformed,
    readable by group/other, or a short key)."""
    path = config_path(private)
    try:
        st = path.stat()
    except OSError:
        return None
    if st.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        log.warning("owner control file is readable by other users; remote runs are off until you "
                    "run `chmod 600 %s` or `python -m pipeline.owner_setup --rotate`", path)
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict) or raw.get("v") != VERSION:
            raise ValueError("version")
        server, topic, key = raw.get("server"), raw.get("topic"), b64d(raw.get("key") or "")
        if not valid_server(server) or not isinstance(topic, str) or not TOPIC_RE.fullmatch(topic) or len(key) != 32:
            raise ValueError("fields")
    except (OSError, ValueError, TypeError, UnicodeDecodeError) as e:
        log.warning("owner control file is invalid (%s); remote runs are off", type(e).__name__)
        return None
    return {"server": server.rstrip("/"), "topic": topic, "key": key}


class NonceStore:
    """Request ids seen in the last 15 minutes, kept in memory and in the private
    folder so a restart can't accept a replay."""

    def __init__(self, path: Path | None = None, ttl: int = NONCE_TTL):
        self.path = Path(path) if path is not None else private_dir() / NONCE_NAME
        self.ttl = ttl
        self.seen: dict[str, float] = {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                self.seen = {str(k): float(v) for k, v in data.items() if isinstance(v, (int, float))}
        except (OSError, ValueError, TypeError):
            pass

    def _prune(self, now: float) -> None:
        self.seen = {k: t for k, t in self.seen.items() if now - t <= self.ttl}

    def has(self, req: str, now: float) -> bool:
        self._prune(now)
        return req in self.seen

    def add(self, req: str, now: float) -> None:
        self._prune(now)
        self.seen[req] = now
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_name(self.path.name + ".tmp")
            tmp.write_text(json.dumps(self.seen), encoding="utf-8")
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.path)
        except OSError as e:
            log.warning("could not save control nonces: %s", type(e).__name__)


class RateLimiter:
    """At most one run in flight, 90 s between accepted runs, 12 per rolling hour."""

    def __init__(self, cooldown: float = COOLDOWN, per_hour: int = PER_HOUR):
        self.cooldown, self.per_hour = cooldown, per_hour
        self.starts: list[float] = []
        self.in_flight = False

    def check(self, now: float) -> str | None:
        self.starts = [t for t in self.starts if now - t < 3600]
        if self.in_flight:
            return "busy"
        if self.starts and now - self.starts[-1] < self.cooldown:
            return "cooldown"
        if len(self.starts) >= self.per_hour:
            return "hourly_cap"
        return None

    def start(self, now: float | None = None) -> None:
        self.starts.append(time.time() if now is None else now)
        self.in_flight = True

    def finish(self) -> None:
        self.in_flight = False


def _is_int(x) -> bool:
    return type(x) is int     # bool is not a valid id or timestamp


def verify(msg_text, cfg: dict, now: float, nonces: NonceStore, limiter: RateLimiter | None = None):
    """(ok, reason, req_dict). An accepted request's id is recorded as seen."""
    if isinstance(msg_text, bytes):
        try:
            msg_text = msg_text.decode("utf-8")
        except UnicodeDecodeError:
            return False, "bad_json", None
    if not isinstance(msg_text, str):
        return False, "bad_json", None
    if len(msg_text.encode("utf-8")) > MAX_BYTES:
        return False, "too_big", None
    try:
        msg = json.loads(msg_text)
    except ValueError:
        return False, "bad_json", None
    if not isinstance(msg, dict):
        return False, "bad_json", None
    if msg.get("v") != VERSION or msg.get("action") != "run":
        return False, "bad_format", None
    slate, req, ts, sig = msg.get("slate_id"), msg.get("req"), msg.get("ts"), msg.get("sig")
    if not _is_int(slate) or not 1 <= slate <= 10 ** 9:
        return False, "bad_slate", None
    if not isinstance(req, str) or not REQ_RE.fullmatch(req):
        return False, "bad_req", None
    if not _is_int(ts) or abs(now - ts) > MAX_SKEW:
        return False, "stale", None
    if not isinstance(sig, str) or not hmac.compare_digest(sign(cfg["key"], slate, req, ts).encode(), sig.encode()):
        return False, "bad_sig", None
    if nonces.has(req, now):
        return False, "replay", None
    # a validly signed request is single-use whatever happens next: recorded before
    # the rate limit, so one refused for busy/cooldown/hourly_cap can't be re-posted later
    nonces.add(req, now)
    if limiter is not None:
        why = limiter.check(now)
        if why:
            return False, why, None
    return True, "ok", {"slate_id": slate, "req": req, "ts": ts}
