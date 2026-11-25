"""Read an injury/lineup news post and decide, conservatively, what it says.

A post counts as a status update only when it names exactly one player on
today's slates (by full name) and its wording maps to exactly one status. Any
post that is ambiguous (several players, mixed wording, no clear status) is
still forwarded to your phone as written, but never changes a projection.
"""
from __future__ import annotations

import re

from .names import norm_name

# order matters: negated / longer phrases are matched and removed first
_PHRASES: list[tuple[str, str]] = [
    (r"\bnot expected to play\b", "OUT"),
    (r"\bruled out\b", "OUT"),
    (r"\bwill not play\b", "OUT"),
    (r"\bwon'?t play\b", "OUT"),
    (r"\bwill miss\b", "OUT"),
    (r"\bnot playing\b", "OUT"),
    (r"\bsits? out\b", "OUT"),
    (r"\bsitting out\b", "OUT"),
    (r"\b(?:is|are|listed as|remains?|downgraded to) out\b", "OUT"),
    (r"\bOUT\b", "OUT"),                         # upper-case OUT, as news accounts write it
    (r"\bdoubtful\b", "DOUBTFUL"),
    (r"\bquestionable\b", "QUESTIONABLE"),
    (r"\bgame[- ]time decision\b", "QUESTIONABLE"),
    (r"\bGTD\b", "QUESTIONABLE"),
    (r"\bprobable\b", "PROBABLE"),
    (r"\bupgraded to available\b", "AVAILABLE"),
    (r"\b(?:is|listed as) available\b", "AVAILABLE"),
    (r"\bavailable to play\b", "AVAILABLE"),
    (r"\b(?:will|expected to|plans to|set to|cleared to) play\b", "AVAILABLE"),
    (r"\bgood to go\b", "AVAILABLE"),
    (r"\b(?:is )?active tonight\b", "AVAILABLE"),
]
_CASE_SENSITIVE = {r"\bOUT\b", r"\bGTD\b"}


def statuses_in(text: str) -> set[str]:
    found, t = set(), text
    for pat, status in _PHRASES:
        flags = 0 if pat in _CASE_SENSITIVE else re.I
        if re.search(pat, t, flags):
            found.add(status)
            t = re.sub(pat, " ", t, flags=flags)
    return found


def players_in(text: str, players: list[dict]) -> list[dict]:
    """Slate players named in full in the post (name matching ignores accents/suffixes)."""
    norm = " " + norm_name(re.sub(r"[^\w\s'.-]", " ", text)) + " "
    hits = {}
    for p in players:
        n = norm_name(p["name"])
        if n and f" {n} " in norm:
            hits[p["key"]] = p
    return list(hits.values())


def parse(text: str, players: list[dict]) -> dict:
    """{'players': [...named], 'status': str|None, 'confident': bool}"""
    named = players_in(text, players)
    st = statuses_in(text)
    confident = len(named) == 1 and len(st) == 1
    return {"players": named, "status": next(iter(st)) if confident else None, "confident": confident}
