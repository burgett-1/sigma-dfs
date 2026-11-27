"""Roster-slot specs built from the registry: the bitmask + Hall's-theorem design
of the original NBA optimizer, generalised to any sport.

Bits are the kind's base positions; a slot's mask is the OR of the positions it
accepts ("*" = all). `hall_need[U]` = number of slots whose accepted positions
all lie inside the position set U, so a set of players fits the slots iff, for
every U, at least hall_need[U] of them can play a position in U.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache

from . import REGISTRY, get


@dataclass(frozen=True)
class SlotSpec:
    sport: str
    kind: str
    positions: tuple
    labels: tuple
    masks: tuple
    mults: tuple
    cap: int
    min_games: int = 1
    min_teams: int = 1
    field_min_salary: int = 0
    pool_min_salary: int = 0
    late_swap: str = "per_game"
    enforce_rules: bool = True
    min_proj: float = 0.0
    stack_rules: dict = field(default_factory=dict, compare=False, hash=False)
    play_prob: dict = field(default_factory=dict, compare=False, hash=False)

    @property
    def size(self) -> int:
        return len(self.labels)

    @property
    def all_mask(self) -> int:
        return (1 << len(self.positions)) - 1

    @property
    def cpt_index(self) -> int | None:
        return next((i for i, m in enumerate(self.mults) if m != 1.0), None)

    @property
    def cpt_mult(self) -> float:
        i = self.cpt_index
        return self.mults[i] if i is not None else 1.0

    @property
    def showdown(self) -> bool:
        return self.cpt_index is not None

    @property
    def hall_need(self) -> dict:
        return hall_need(self.masks, len(self.positions))

    def bit(self, pos: str) -> int:
        return 1 << self.positions.index(pos) if pos in self.positions else 0


def hall_need(masks: tuple, k: int) -> dict:
    """{U: need(U)} over every non-empty position subset U < 2^k (same form as
    the legacy sim.HALL_NEED, which ranges U over 1..30 for NBA's 5 bits)."""
    return _hall_need(tuple(masks), k)


@lru_cache(maxsize=None)
def _hall_need(masks: tuple, k: int) -> dict:
    return {U: sum(1 for m in masks if (m & ~U) == 0) for U in range(1, (1 << k) - 1)}


def _mask(pos, positions: tuple) -> int:
    if pos == "*":
        return (1 << len(positions)) - 1
    m = 0
    for p in pos:
        m |= 1 << positions.index(p)
    return m


@lru_cache(maxsize=None)
def _spec_cached(sport: str, kind: str, reg_id: int) -> SlotSpec:
    reg = _REGS[reg_id]
    s = get(sport, reg)
    k = (s.get("kinds") or {}).get(kind)
    if k is None:
        raise KeyError(f"{sport} has no kind {kind!r}")
    positions = tuple(k["positions"])
    proj = s.get("projection") or {}
    return SlotSpec(
        sport=sport, kind=kind, positions=positions,
        labels=tuple(sl["label"] for sl in k["slots"]),
        masks=tuple(_mask(sl["pos"], positions) for sl in k["slots"]),
        mults=tuple(float(sl.get("mult", 1.0)) for sl in k["slots"]),
        cap=int(k["cap"]), min_games=int(k.get("min_games", 1)), min_teams=int(k.get("min_teams", 1)),
        field_min_salary=int(k.get("field_min_salary", 0)), pool_min_salary=int(k.get("pool_min_salary", 0)),
        late_swap=k.get("late_swap", "per_game"), enforce_rules=bool(k.get("enforce_rules", True)),
        min_proj=float(proj.get("min_proj", 0.0)),
        stack_rules=dict(k.get("stack_rules") or {}), play_prob=dict(s.get("play_prob") or {}),
    )


_REGS: dict[int, dict] = {id(REGISTRY): REGISTRY}


def spec(sport: str, kind: str = "classic", reg: dict | None = None) -> SlotSpec:
    reg = reg or REGISTRY
    _REGS.setdefault(id(reg), reg)
    return _spec_cached(sport, kind, id(reg))


def pos_code(tokens, sp: SlotSpec) -> int:
    """OR of the bits for tokens in sp.positions. Unknown or none -> 0 (fail closed)."""
    if isinstance(tokens, str):
        tokens = tokens.upper().split("/")
    code = 0
    for t in tokens or ():
        t = str(t).strip().upper()
        if t in sp.positions:
            code |= 1 << sp.positions.index(t)
    return code


def eligibility(roster_position: str, position: str, sp: SlotSpec) -> dict | None:
    """Slot eligibility of one salary-CSV row, or None (excluded: fail closed).

    classic: the player's position code = Roster Position tokens within the kind's
    positions; the slots that code implies must equal the CSV's slot tokens.
    showdown: the code comes from the Position column; every slot accepts all."""
    rp = [t.strip().upper() for t in (roster_position or "").split("/") if t.strip()]
    if sp.showdown:
        code = pos_code(position, sp)
        if not code:
            return None
        return {"code": code, "base": [p for p in sp.positions if code & sp.bit(p)],
                "slots": sorted(set(sp.labels))}
    base = [t for t in rp if t in sp.positions]
    code = pos_code(base, sp)
    if not code:
        return None
    expected = {lab for lab, m in zip(sp.labels, sp.masks) if m & code}
    in_csv = {t for t in rp if t in set(sp.labels)}
    unknown = [t for t in rp if t not in sp.positions and t not in set(sp.labels)]
    if unknown or expected != in_csv:
        return None
    return {"code": code, "base": [p for p in sp.positions if code & sp.bit(p)],
            "slots": [lab for lab in dict.fromkeys(sp.labels) if lab in expected]}


@lru_cache(maxsize=200_000)
def can_fill(codes: tuple, masks: tuple) -> bool:
    """Can players with these codes occupy distinct slots of `masks`? (augmenting paths)"""
    if len(codes) > len(masks):
        return False
    match: dict[int, int] = {}

    def try_player(i: int, seen: set) -> bool:
        for s, m in enumerate(masks):
            if s in seen or not (codes[i] & m):
                continue
            seen.add(s)
            if s not in match or try_player(match[s], seen):
                match[s] = i
                return True
        return False

    return all(try_player(i, set()) for i in range(len(codes)))


def assign(codes: list[int], masks: tuple, order: list[int] | None = None) -> list[int] | None:
    """Slot index -> player index (into `codes`), filling slots in `order`
    (default: last slot first, so flex slots take the first players offered)."""
    order = list(order if order is not None else range(len(masks) - 1, -1, -1))
    res: dict[int, int] = {}

    def bt(k: int, used: set) -> bool:
        if k == len(order):
            return True
        s = order[k]
        for i, c in enumerate(codes):
            if i in used or not (c & masks[s]):
                continue
            used.add(i)
            res[s] = i
            if bt(k + 1, used):
                return True
            used.discard(i)
        return False

    if not bt(0, set()):
        return None
    return [res[s] for s in range(len(masks))]


def lineup_legal(players: list[dict], sp: SlotSpec, stack: bool = True) -> bool:
    """DraftKings legality (+ registry stack rules) of a complete lineup in slot
    order. players: [{id, code, salary, cpt_salary?, team, opp, game}]. The web
    twin is sports.js lineupLegal; the conformance fixture checks they agree."""
    if not players or len(players) != sp.size:
        return False
    if len({p["id"] for p in players}) != len(players):
        return False
    salary = 0
    for p, m, mult in zip(players, sp.masks, sp.mults):
        if not (p["code"] & m):
            return False
        salary += (p.get("cpt_salary") or int(p["salary"] * mult + 0.5)) if mult != 1.0 else p["salary"]
    if salary > sp.cap:
        return False
    if not sp.enforce_rules:
        return True
    if len({p["team"] for p in players}) < sp.min_teams:
        return False
    if sp.showdown:
        return True
    if len({p["game"] for p in players}) < sp.min_games:
        return False
    sr = sp.stack_rules
    if not sr:
        return True
    qb, dst = sp.bit("QB"), sp.bit("DST")
    catch = sp.bit("WR") | sp.bit("TE")
    skill = catch | sp.bit("RB")
    qbs = [p for p in players if p["code"] & qb]
    if sr.get("no_dst_vs_qb") and dst:
        for d in (p for p in players if p["code"] & dst):
            if any(q["opp"] == d["team"] for q in qbs):
                return False
    if not stack:
        return True
    m, b = int(sr.get("qb_stack_min", 0)), int(sr.get("bring_back_min", 0))
    for q in qbs:
        if m and sum(1 for p in players if p is not q and p["code"] & catch and p["team"] == q["team"]) < m:
            return False
        if b and sum(1 for p in players if p["code"] & skill and p["team"] == q["opp"]) < b:
            return False
    return True


def slot_columns_match(cells: list[str], sp: SlotSpec) -> bool:
    labels = set(sp.labels)
    got = sorted(c for c in cells if c in labels)
    return got == sorted(sp.labels)


def detect_entries_sport(header_cells: list[str], reg: dict | None = None) -> dict | None:
    """{sport, kind} whose slot labels equal a DK entries header's slot columns
    (as a multiset); enabled sports first. Web twin: sports.js detectEntriesSport."""
    from . import REGISTRY
    reg = reg or REGISTRY
    cells = [str(c).strip().upper() for c in header_cells or []]
    if "ENTRY ID" not in cells:
        return None
    order = sorted(reg["sports"], key=lambda c: 0 if reg["sports"][c].get("enabled") else 1)
    for code in order:
        for kind in reg["sports"][code].get("kinds") or {}:
            if slot_columns_match(cells, spec(code, kind, reg)):
                return {"sport": code, "kind": kind}
    return None
