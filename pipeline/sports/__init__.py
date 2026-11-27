"""Sport registry: one canonical file (web/src/sports/registry.json) read by the
pipeline and the web app, so the two sides can't drift.

    from pipeline import sports
    sports.get("NFL")            # the registry entry (dict)
    sports.enabled()             # codes that run, honouring SIGMA_SPORTS
    sports.in_season("NFL", now) # inside the season windows?

The registry is validated at import; a malformed file raises immediately
instead of producing illegal lineups later.
"""
from __future__ import annotations

import json
import os
from datetime import date, datetime
from pathlib import Path

from ..config import ET, ROOT

REGISTRY_PATH = ROOT / "web" / "src" / "sports" / "registry.json"


class RegistryError(ValueError):
    pass


def validate(reg: dict) -> list[str]:
    """Every rule the web validator (sports.js validateRegistry) applies, same order.
    Returns the list of problems (empty = valid)."""
    errs: list[str] = []
    if not isinstance(reg, dict) or not isinstance(reg.get("sports"), dict):
        return ["registry must be an object with a 'sports' object"]
    codes = set()
    for key, s in reg["sports"].items():
        if not isinstance(s, dict):
            errs.append(f"{key}: entry must be an object")
            continue
        code = s.get("code")
        if code != key:
            errs.append(f"{key}: code {code!r} must equal its key")
        if code in codes:
            errs.append(f"{key}: duplicate code")
        codes.add(code)
        for gt in (s.get("game_types") or {}):
            if not str(gt).isdigit():
                errs.append(f"{key}: game_types key {gt!r} is not an integer string")
        for kname, k in (s.get("kinds") or {}).items():
            pos = k.get("positions") or []
            if not (isinstance(k.get("cap"), (int, float)) and k["cap"] > 0):
                errs.append(f"{key}.{kname}: cap must be > 0")
            for sl in k.get("slots") or []:
                p = sl.get("pos")
                if p != "*" and (not isinstance(p, list) or not p or not set(p) <= set(pos)):
                    errs.append(f"{key}.{kname}: slot {sl.get('label')} positions {p!r} not within {pos}")
            if kname == "showdown":
                n_mult = sum(1 for sl in k.get("slots") or [] if sl.get("mult"))
                if n_mult != 1:
                    errs.append(f"{key}.{kname}: showdown needs exactly one multiplier slot (has {n_mult})")
        corr = s.get("corr") or {}
        if corr.get("model") == "factor":
            for p, ld in (corr.get("loadings") or {}).items():
                ss = sum(float(v) ** 2 for v in ld.values())
                if ss >= 0.95:
                    errs.append(f"{key}: loadings for {p} have sum of squares {ss:.3f} >= 0.95")
    return errs


def load(path: Path | None = None) -> dict:
    reg = json.loads(Path(path or REGISTRY_PATH).read_text(encoding="utf-8"))
    errs = validate(reg)
    if errs:
        raise RegistryError("; ".join(errs))
    return reg


REGISTRY = load()


def get(code: str, reg: dict | None = None) -> dict:
    sports = (reg or REGISTRY)["sports"]
    if code not in sports:
        raise KeyError(f"unknown sport {code!r}")
    return sports[code]


def codes(reg: dict | None = None) -> list[str]:
    return list((reg or REGISTRY)["sports"])


def enabled(reg: dict | None = None, env: str | None = None) -> list[str]:
    """Sports that run. SIGMA_SPORTS (comma list) overrides the registry's
    `enabled` flags; unknown codes are ignored. NBA is the kill-switch default:
    SIGMA_SPORTS=NBA restores the single-sport pipeline exactly."""
    reg = reg or REGISTRY
    raw = os.environ.get("SIGMA_SPORTS", "") if env is None else env
    if raw.strip():
        want = [c.strip().upper() for c in raw.split(",") if c.strip()]
        return [c for c in want if c in reg["sports"]]
    return [c for c, s in reg["sports"].items() if s.get("enabled")]


def _md(s: str) -> tuple[int, int]:
    m, d = s.split("-")
    return int(m), int(d)


def in_season(code: str, when: datetime | date, reg: dict | None = None) -> bool:
    """Is `when` (ET date) inside one of the sport's season windows? A window may
    wrap the new year (NFL 09-01 .. 02-15)."""
    s = get(code, reg)
    d = when.astimezone(ET).date() if isinstance(when, datetime) else when
    md = (d.month, d.day)
    for lo, hi in (s.get("season") or {}).get("windows") or []:
        a, b = _md(lo), _md(hi)
        if (a <= md <= b) if a <= b else (md >= a or md <= b):
            return True
    return False


def kind_for_game_type(code: str, game_type_id, reg: dict | None = None) -> dict | None:
    """{'kind', 'csv_ct'} for a DraftKings GameTypeId, or None if not built for."""
    return (get(code, reg).get("game_types") or {}).get(str(game_type_id))
