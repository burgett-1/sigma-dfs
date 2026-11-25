"""Fast news (X accounts such as @Newzgod_dfs) on top of the verifiable sources.

  * The poller (live.py) checks the accounts every few seconds, pushes any post
    naming a slate player to your phone immediately, and stores it privately.
  * A post that clearly gives one player one status (news_parse.py) sets that
    status right away. The official report / ESPN take over again as soon as
    they change that player's status afterwards (the newest information wins).
  * Speed log (private): for every status both sides reported, when the post
    went up versus when our official sources first showed it.

Everything here lives in the private folder (~/SigmaDFS-Private/news), never in
the published data: the posts belong to their authors.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime, timezone

from .config import DATA_DIR, ET
from .contests import PRIVATE
from .names import player_key

log = logging.getLogger(__name__)

NEWS_DIR = PRIVATE / "news"
ITEMS = NEWS_DIR / "x_items.json"
SPEED = NEWS_DIR / "speed.json"
_LOCK = threading.Lock()


def _read(path, default):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return default


def _write(path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(obj, indent=1))
    tmp.replace(path)


def slate_players() -> list[dict]:
    """Today's projected players (name, team, key) from the published projections."""
    doc = _read(DATA_DIR / "projections.json", {})
    out = []
    for r in doc.get("players", []):
        team = r.get("team") or r.get("team_dk") or ""
        out.append({"name": r["name"], "team": team, "key": player_key(r["name"], team)})
    return out


def record(items: list[dict]) -> None:
    with _LOCK:
        cur = _read(ITEMS, [])
        have = {i["id"] for i in cur}
        cur += [i for i in items if i["id"] not in have]
        _write(ITEMS, cur[-400:])


def _today_items(now: datetime) -> list[dict]:
    day = now.astimezone(ET).date()
    with _LOCK:
        items = _read(ITEMS, [])
    return [i for i in items if datetime.fromisoformat(i["at"]).astimezone(ET).date() == day]


def apply(base: dict[str, dict], state: dict, now: datetime) -> dict[str, dict]:
    """base: player_key -> {status, reason, source} from the official report / ESPN.
    Returns the statuses with clear X posts applied where they are newer."""
    seen = state.setdefault("base_status", {})
    changed_at = state.setdefault("base_changed_at", {})
    logged = set(state.setdefault("speed_logged", []))
    now_iso = now.isoformat()
    for k, rec in base.items():
        if seen.get(k) != rec.get("status", ""):
            if k in seen:
                changed_at[k] = now_iso
            seen[k] = rec.get("status", "")
    out = {k: dict(v) for k, v in base.items()}
    speed = []
    for it in _today_items(now):
        if not it.get("confident"):
            continue
        k, st = it["players"][0], ("" if it["status"] == "AVAILABLE" else it["status"])
        if k not in out:
            continue
        base_st, base_t = seen.get(k, ""), changed_at.get(k)
        # speed: both sides have now reported the same status for this player
        tag = f"{k}|{st}|{it['id']}"
        if base_st == st and tag not in logged and base_t:
            speed.append({"player": it.get("names", [k])[0], "status": st or "AVAILABLE", "account": it["account"],
                          "x_posted": it["at"], "x_seen": it.get("seen_at"), "official_seen": base_t,
                          "official_source": base.get(k, {}).get("source", ""),
                          "lead_seconds": round((datetime.fromisoformat(base_t) - datetime.fromisoformat(it["at"])).total_seconds())})
            logged.add(tag)
        # the newest information wins: the post, unless the official side changed since
        if not base_t or base_t < it["at"]:
            out[k] = {"status": st, "reason": it["text"][:200], "source": f"X @{it['account']}"}
    state["speed_logged"] = sorted(logged)[-2000:]
    if speed:
        with _LOCK:
            _write(SPEED, (_read(SPEED, []) + speed)[-3000:])
    return out


def speed_summary() -> dict:
    rows = _read(SPEED, [])
    if not rows:
        return {"items": 0}
    leads = sorted(r["lead_seconds"] for r in rows)
    first = sum(1 for v in leads if v > 0)
    return {"items": len(rows), "x_first": first, "official_first": len(rows) - first,
            "median_lead_seconds": leads[len(leads) // 2]}


class Poller(threading.Thread):
    """Background thread: polls the X accounts every POLL seconds."""
    POLL = 5.0

    def __init__(self, notify_fn):
        super().__init__(daemon=True)
        self.notify_fn = notify_fn
        self.event = threading.Event()        # set when a post changes something
        self.state_path = NEWS_DIR / "poller.json"

    def run(self) -> None:
        from .sources import x_news
        from . import news_parse
        st = _read(self.state_path, {})
        users, wait_until = st.get("users", {}), 0.0
        while True:
            time.sleep(self.POLL)
            if time.time() < wait_until or not x_news.token():
                continue
            try:
                players = slate_players()
                for acct in x_news.accounts():
                    if acct not in users:
                        users[acct] = {"id": x_news.user_id(acct), "since": None}
                    u = users[acct]
                    posts = x_news.new_posts(u["id"], u["since"])
                    first = u["since"] is None
                    if posts:
                        u["since"] = posts[-1]["id"]
                    if first:
                        continue                     # starting point only; don't replay old posts
                    items = []
                    for p in posts:
                        res = news_parse.parse(p["text"], players)
                        if not res["players"]:
                            continue
                        items.append({**p, "account": acct, "seen_at": datetime.now(timezone.utc).isoformat(),
                                      "players": [q["key"] for q in res["players"]],
                                      "names": [q["name"] for q in res["players"]],
                                      "status": res["status"], "confident": res["confident"]})
                    if items:
                        record(items)
                        for i in items:
                            self.notify_fn(f"@{acct}: {', '.join(i['names'])}", i["text"][:300])
                        self.event.set()
                _write(self.state_path, {"users": users})
            except x_news.XError as e:
                log.warning("X news: %s", e)
                wait_until = x_news.backoff_until(e)
            except Exception as e:  # noqa: BLE001
                log.warning("X news poll failed: %s", e)
                wait_until = time.time() + 30
