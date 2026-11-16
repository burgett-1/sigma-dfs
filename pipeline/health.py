"""Per-run health of every external data source (shown in meta.json and the app),
so a blocked or changed source is visible immediately instead of failing silently."""
from __future__ import annotations

from datetime import datetime, timezone

_status: dict[str, dict] = {}


def ok(source: str, detail: str = "") -> None:
    _status[source] = {"ok": True, "detail": str(detail)[:160], "at": datetime.now(timezone.utc).isoformat()}


def fail(source: str, err) -> None:
    _status[source] = {"ok": False, "detail": str(err)[:160], "at": datetime.now(timezone.utc).isoformat()}


def snapshot() -> dict:
    return dict(_status)
