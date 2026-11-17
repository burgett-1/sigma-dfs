"""Sharp betting markets via The Odds API (the-odds-api.com, licensed feed, API key).

Game lines (moneyline, spread, total) and player props (points, rebounds,
assists, threes, blocks, steals, turnovers) from the sharpest books first:
Pinnacle, then low-margin books, then the major US books. Prices are de-vigged
(bookmaker margin removed) and turned into the market's expected value, e.g.
"Over 27.5 at 58% fair" -> about 28.6 expected points.
"""
from __future__ import annotations

import logging
import math
from statistics import NormalDist

from ..http import session
from ..names import norm_name, team_code

log = logging.getLogger(__name__)

BASE = "https://api.the-odds-api.com/v4/sports/basketball_nba"
# sharpest first; weights used when several books post the same market
BOOK_WEIGHT = {"pinnacle": 4.0, "circasports": 3.0, "lowvig": 2.5, "betonlineag": 2.0, "bookmaker": 2.0,
               "fanduel": 1.2, "draftkings": 1.0, "betmgm": 0.8, "williamhill_us": 0.8, "espnbet": 0.8,
               "betrivers": 0.7, "bovada": 0.7}
BOOKS = ",".join(list(BOOK_WEIGHT)[:10])     # 10 books = billed as one region
PROP_MARKETS = {"player_points": "pts", "player_rebounds": "reb", "player_assists": "ast",
                "player_threes": "fg3m", "player_blocks": "blk", "player_steals": "stl",
                "player_turnovers": "tov"}
# game-to-game spread of each stat relative to sqrt(line) (NBA box scores)
STAT_SPREAD = {"pts": 1.5, "reb": 1.1, "ast": 1.1, "fg3m": 1.0, "blk": 1.0, "stl": 1.0, "tov": 1.0}
MARGIN_SD, TOTAL_SD = 12.5, 18.0
_N = NormalDist()

last_usage: dict = {}


def _get(path: str, key: str, **params):
    r = session().get(f"{BASE}{path}", params={"apiKey": key, **params}, timeout=20)
    r.raise_for_status()
    last_usage.update({"remaining": r.headers.get("x-requests-remaining"),
                       "used": r.headers.get("x-requests-used")})
    return r.json()


def american_to_prob(price: float) -> float:
    price = float(price)
    return 100.0 / (price + 100.0) if price > 0 else -price / (-price + 100.0)


def devig(p_a: float, p_b: float) -> float:
    """Fair probability of side A from both sides' implied probabilities."""
    s = p_a + p_b
    return p_a / s if s > 0 else 0.5


def implied_mean(line: float, p_over: float, sd: float) -> float:
    """Market expectation from an over/under: P(X > line) = p_over with X ~ N(mean, sd)."""
    p = min(max(p_over, 0.02), 0.98)
    return line + sd * _N.inv_cdf(p)


# ── game lines ──────────────────────────────────────────────────────────────
def game_lines(key: str) -> list[dict]:
    data = _get("/odds", key, bookmakers=BOOKS, markets="h2h,spreads,totals", oddsFormat="american")
    return [parse_game(ev) for ev in data]


def parse_game(ev: dict) -> dict:
    home, away = ev.get("home_team", ""), ev.get("away_team", "")
    books = {}
    margins, totals, wps = [], [], []
    for b in ev.get("bookmakers") or []:
        w = BOOK_WEIGHT.get(b.get("key"), 0.5)
        entry = {"title": b.get("title"), "updated": b.get("last_update")}
        for m in b.get("markets") or []:
            oc = {o.get("name"): o for o in m.get("outcomes") or []}
            if m.get("key") == "h2h" and home in oc and away in oc:
                ph = devig(american_to_prob(oc[home]["price"]), american_to_prob(oc[away]["price"]))
                entry["ml"] = {"home": oc[home]["price"], "away": oc[away]["price"], "home_fair": round(ph, 4)}
                wps.append((ph, w))
            elif m.get("key") == "spreads" and home in oc and away in oc:
                pt = float(oc[home]["point"])
                ph = devig(american_to_prob(oc[home]["price"]), american_to_prob(oc[away]["price"]))
                # home covers -pt: margin > -pt; expected home margin from the fair cover probability
                margins.append((-pt + MARGIN_SD * _N.inv_cdf(min(max(ph, 0.02), 0.98)), w))
                entry["spread"] = {"home": pt, "home_price": oc[home]["price"], "away_price": oc[away]["price"]}
            elif m.get("key") == "totals" and "Over" in oc and "Under" in oc:
                pt = float(oc["Over"]["point"])
                po = devig(american_to_prob(oc["Over"]["price"]), american_to_prob(oc["Under"]["price"]))
                totals.append((implied_mean(pt, po, TOTAL_SD), w))
                entry["total"] = {"point": pt, "over_price": oc["Over"]["price"], "under_price": oc["Under"]["price"]}
        books[b.get("key")] = entry

    def wavg(xs):
        sw = sum(w for _, w in xs)
        return sum(x * w for x, w in xs) / sw if sw else None

    margin, total, wp = wavg(margins), wavg(totals), wavg(wps)
    out = {"event_id": ev.get("id"), "commence": ev.get("commence_time"),
           "home": team_code(home), "away": team_code(away), "books": books,
           "home_margin": margin, "total": total, "home_win": wp,
           "sharpest": next((k for k in BOOK_WEIGHT if k in books), None)}
    if margin is not None and total is not None:
        out["home_implied"] = total / 2 + margin / 2
        out["away_implied"] = total / 2 - margin / 2
    return out


def team_odds(games: list[dict]) -> dict[str, dict]:
    """{team: {spread, total, implied, opp}} in the format the model uses."""
    out = {}
    for g in games:
        if g.get("home_implied") is None:
            continue
        out[g["home"]] = {"spread": -g["home_margin"], "total": g["total"], "implied": g["home_implied"],
                          "opp": g["away"], "source": "sharp books"}
        out[g["away"]] = {"spread": g["home_margin"], "total": g["total"], "implied": g["away_implied"],
                          "opp": g["home"], "source": "sharp books"}
    return out


# ── player props ────────────────────────────────────────────────────────────
def player_props(key: str, event_id: str, markets: list[str] | None = None) -> dict:
    mk = ",".join(markets or PROP_MARKETS)
    return _get(f"/events/{event_id}/odds", key, bookmakers=BOOKS, markets=mk, oddsFormat="american")


def parse_props(ev: dict) -> dict[str, dict]:
    """{norm player name: {stat: {mean, line, over_fair, books, best_over, best_under}}}"""
    acc: dict[tuple, list] = {}
    for b in ev.get("bookmakers") or []:
        w = BOOK_WEIGHT.get(b.get("key"), 0.5)
        for m in b.get("markets") or []:
            stat = PROP_MARKETS.get(m.get("key"))
            if not stat:
                continue
            pairs: dict[tuple, dict] = {}
            for o in m.get("outcomes") or []:
                pairs.setdefault((o.get("description"), float(o.get("point", 0))), {})[o.get("name")] = o.get("price")
            for (player, point), sides in pairs.items():
                if "Over" not in sides or "Under" not in sides:
                    continue
                po = devig(american_to_prob(sides["Over"]), american_to_prob(sides["Under"]))
                acc.setdefault((norm_name(player), player, stat), []).append(
                    (point, po, w, b.get("key"), sides["Over"], sides["Under"]))
    out: dict[str, dict] = {}
    for (nk, player, stat), rows in acc.items():
        sw = sum(r[2] for r in rows)
        means = [implied_mean(r[0], r[1], STAT_SPREAD[stat] * math.sqrt(max(r[0], 0.5))) for r in rows]
        mean = sum(mu * r[2] for mu, r in zip(means, rows)) / sw
        main = max(rows, key=lambda r: r[2])                    # sharpest book's line
        best_over = max(rows, key=lambda r: r[4])
        best_under = max(rows, key=lambda r: r[5])
        out.setdefault(nk, {"name": player})[stat] = {
            "mean": round(mean, 2), "line": main[0], "over_fair": round(main[1], 4), "book": main[3],
            "books": len(rows), "best_over": [best_over[3], best_over[0], best_over[4]],
            "best_under": [best_under[3], best_under[0], best_under[5]],
        }
    return out
