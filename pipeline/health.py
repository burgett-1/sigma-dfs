"""Per-run health of every external data source (shown in meta.json and the app),
so a blocked or changed source is visible immediately instead of failing silently.

Sources are recorded in the global (NBA) table unless a sport scope is active:

    with health.scope("NFL"):
        health.ok("nflverse")          # -> snapshot("NFL"), never the NBA table
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone

_status: dict[str, dict] = {}
_scoped: dict[str, dict[str, dict]] = {}
_active: list[str] = []


def _table() -> dict[str, dict]:
    return _scoped.setdefault(_active[-1], {}) if _active else _status


def ok(source: str, detail: str = "") -> None:
    _table()[source] = {"ok": True, "detail": str(detail)[:160], "at": datetime.now(timezone.utc).isoformat()}


def fail(source: str, err) -> None:
    _table()[source] = {"ok": False, "detail": str(err)[:160], "at": datetime.now(timezone.utc).isoformat()}


@contextmanager
def scope(sport: str):
    """Route ok/fail inside the block to the sport's own table."""
    _active.append(sport)
    try:
        yield
    finally:
        _active.pop()


def default_scope(sport: str):
    """scope(sport) unless a scope is already active (or the sport is NBA): for
    non-NBA code that may also be called directly, outside a runner."""
    from contextlib import nullcontext
    return scope(sport) if sport != "NBA" and not _active else nullcontext()


def snapshot(sport: str | None = None) -> dict:
    """The NBA/global table when sport is None (today's behaviour), else the sport's."""
    if sport is None:
        return dict(_status)
    return dict(_scoped.get(sport, {}))
