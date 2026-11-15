"""Shared HTTP session with retries and polite headers."""
from __future__ import annotations

import logging
import time

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .config import HTTP_TIMEOUT, USER_AGENT

log = logging.getLogger(__name__)
_session: requests.Session | None = None


def session() -> requests.Session:
    global _session
    if _session is None:
        s = requests.Session()
        retry = Retry(
            total=3, backoff_factor=1.0,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=("GET", "HEAD", "POST"),
        )
        s.mount("https://", HTTPAdapter(max_retries=retry, pool_maxsize=16))
        s.headers.update({
            "User-Agent": USER_AGENT,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-US,en;q=0.9",
        })
        _session = s
    return _session


def get_json(url: str, **kw):
    r = session().get(url, timeout=kw.pop("timeout", HTTP_TIMEOUT), **kw)
    r.raise_for_status()
    return r.json()


def get_bytes(url: str, **kw) -> bytes | None:
    """GET returning body bytes, or None on 403/404 (used for URL probing)."""
    r = session().get(url, timeout=kw.pop("timeout", HTTP_TIMEOUT), **kw)
    if r.status_code in (403, 404):
        return None
    r.raise_for_status()
    return r.content


def polite_sleep(seconds: float = 0.15) -> None:
    time.sleep(seconds)
