"""Breaking news from X accounts (default: @Newzgod_dfs), via the official X API.

Needs an X API bearer token in ~/.sigma-dfs.env:  X_BEARER_TOKEN=...
(X developer console, pay-per-use credits). Other accounts can be added with
NEWS_X_ACCOUNTS=Newzgod_dfs,OtherAccount.

Cost control: each poll asks only for posts newer than the last one seen
(since_id). X charges per post returned (docs.x.com pricing: $0.005 per post
read), so an empty poll returns nothing and a typical day costs roughly
$0.005 x the number of posts the account makes. Post times are X's own
created_at field.
"""
from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timezone

from ..http import session

log = logging.getLogger(__name__)

API = "https://api.x.com/2"
_X_EPOCH_MS = 1288834974657


def post_time(post_id: str | int) -> datetime:
    """Creation time encoded in an X post ID (snowflake: ms since X's epoch << 22)."""
    ms = (int(post_id) >> 22) + _X_EPOCH_MS
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)


def token() -> str:
    return os.environ.get("X_BEARER_TOKEN", "").strip()


def accounts() -> list[str]:
    raw = os.environ.get("NEWS_X_ACCOUNTS", "Newzgod_dfs")
    return [a.strip().lstrip("@") for a in raw.split(",") if a.strip()]


class XError(Exception):
    def __init__(self, status: int, msg: str, retry_at: float | None = None):
        super().__init__(f"X API {status}: {msg}")
        self.status, self.retry_at = status, retry_at


def _get(path: str, params: dict | None = None) -> dict:
    r = session().get(f"{API}{path}", params=params or {}, timeout=10,
                      headers={"Authorization": f"Bearer {token()}"})
    if r.status_code != 200:
        reset = r.headers.get("x-rate-limit-reset")
        raise XError(r.status_code, r.text[:200], float(reset) if reset and reset.isdigit() else None)
    return r.json()


def user_id(username: str) -> str:
    return _get(f"/users/by/username/{username}")["data"]["id"]


_FIELDS_PARAM = ["post.fields"]          # current docs; older API versions call it tweet.fields


def new_posts(uid: str, since_id: str | None) -> list[dict]:
    """Posts newer than since_id, oldest first: [{id, text, at}]. On the very first
    call (no since_id) only the latest few are fetched, to set the starting point."""
    params = {"max_results": 20 if since_id else 5, "exclude": "retweets", _FIELDS_PARAM[0]: "created_at"}
    if since_id:
        params["since_id"] = since_id
    try:
        data = _get(f"/users/{uid}/tweets", params).get("data") or []
    except XError as e:
        if e.status != 400 or _FIELDS_PARAM[0] == "tweet.fields":
            raise
        _FIELDS_PARAM[0] = "tweet.fields"
        params["tweet.fields"] = params.pop("post.fields")
        data = _get(f"/users/{uid}/tweets", params).get("data") or []
    out = []
    for p in data:
        at = p.get("created_at")
        at = datetime.fromisoformat(at.replace("Z", "+00:00")) if at else post_time(p["id"])
        out.append({"id": p["id"], "text": p.get("text", ""), "at": at.isoformat()})
    return sorted(out, key=lambda p: int(p["id"]))


def backoff_until(e: XError) -> float:
    if e.status == 429:
        return e.retry_at or time.time() + 60
    if e.status in (401, 402, 403):          # bad token / no credits: don't hammer the API
        return time.time() + 600
    return time.time() + 30
