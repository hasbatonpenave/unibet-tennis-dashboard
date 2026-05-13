"""
unibet_api.py — Unibet.fr Sports API Client
=============================================
Reverse-engineered API endpoints for the Unibet/Kindred sportsbook platform.

Endpoints discovered:
  GET  /lvs-api/acc/token                                          → Auth token
  GET  /services-api/sportsbookdata/current/events/live/topmarket  → Live events + odds (all sports)
  GET  /services-api/sportsbookdata/delta/events/live/topmarket/from/{basket}  → Delta updates
  GET  /lvs-api/next/50/p{pathId}?...                              → Event listing (no odds)
  GET  /paris-tennis/{cat}/{league}/{id}/{slug}                    → SSR match detail w/ odds
  POST /service-sport-enligne-bff/v1/outcomes-percents             → Betting probability % by outcome
  GET  /service-sport-enligne-bff/v1/quick-access                  → Popular competition IDs

Data model (flat items dict, keyed by prefix):
  l<N>  — live event     {desc, a, b, code, score, set[], match[], period, parent=p<id>}
  e<N>  — prematch event  {desc, eType=G|R, start, parent, path, opponentA/B}
  m<N>  — market          {parent=e<id>, markettypeId, desc, style, periodId, cashout}
  o<N>  — outcome         {parent=m<id>, price, desc, pos, spread, suspended}
  p<N>  — competition     {desc, eType, items: {e<>...}}

Tennis (code=TENN):
  markettypeId 68  = "Face à Face" (Moneyline — 2-way)
  markettypeId 69  = "Vainqueur du set X" (Set winner)
  markettypeId 361 = "Total des jeux" (Total games O/U)
  markettypeId 840 = "Score Exact" (Exact score)

Football (code=FOOT):
  markettypeId 1   = "1 N 2" (Match result — 3-way home/draw/away)
  score            = {a: int, b: int}  (home/away goals)
  period           = {type, duration, instance, desc}  (e.g. "1ère Période")
  time             = {m: int, s: int}  (elapsed minutes/seconds)

Auth:
  X-LVS-HSToken header required for all /services-api/ and /lvs-api/ endpoints.
  Token obtained from /lvs-api/acc/token (session) or SSR serverApp-state (persistent).
"""

import asyncio
import json
import logging
import re
import ssl
import struct
import sys
import time
from datetime import datetime, timezone
from typing import Any

import aiohttp

logger = logging.getLogger("unibet_api")

# ── CONFIG ──────────────────────────────────────────────────────────────────────
BASE_URL = "https://www.unibet.fr"
LVS_TOKEN_URL = f"{BASE_URL}/lvs-api/acc/token"
LIVE_EVENTS_URL = f"{BASE_URL}/services-api/sportsbookdata/current/events/live/topmarket"
LIVE_DELTA_URL = f"{BASE_URL}/services-api/sportsbookdata/delta/events/live/topmarket/from"
EVENT_LISTING_URL = f"{BASE_URL}/lvs-api/next/50"
OUTCOMES_PERCENTS_URL = f"{BASE_URL}/service-sport-enligne-bff/v1/outcomes-percents"
QUICK_ACCESS_URL = f"{BASE_URL}/service-sport-enligne-bff/v1/quick-access"

# Sport path IDs (from SSR Ept tree)
TENNIS_PATH_ID   = "p239"       # All Tennis
ATP_PATH_ID      = "p58484924"  # ATP category
WTA_PATH_ID      = "p58484929"  # WTA category
FOOTBALL_PATH_ID = "p240"       # All Football

# Tennis market type IDs
MARKET_FACE_A_FACE     = 68    # Moneyline / Match Winner (2-way)
MARKET_SET_WINNER      = 69    # Set X winner
MARKET_TOTAL_GAMES     = 361   # Total games O/U
MARKET_EXACT_SCORE     = 840   # Exact score
MARKET_GAME_HANDICAP   = 130010
MARKET_SET_BETTING     = 10098 # Number of sets
MARKET_WIN_ONE_SET     = 120997  # Player to win at least 1 set

# Football market type IDs
MARKET_1X2 = 1   # Match result: home / draw / away (3-way)

# Set of moneyline market IDs across all tracked sports
MONEYLINE_MARKET_TYPES = {MARKET_FACE_A_FACE, MARKET_1X2}

# Market names to extract
TARGET_MARKETS = {
    MARKET_FACE_A_FACE: "Face à Face",
    MARKET_1X2:         "1 N 2",
}

HEADERS_TEMPLATE = {
    "accept":             "application/json, text/plain, */*",
    "accept-language":    "fr,en-US;q=0.9,en;q=0.8",
    "dnt":                "1",
    "origin":             BASE_URL,
    "referer":            f"{BASE_URL}/paris-tennis",
    "sec-fetch-dest":     "empty",
    "sec-fetch-mode":     "cors",
    "sec-fetch-site":     "same-origin",
    "user-agent":         (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36"
    ),
}

# ── TOKEN MANAGEMENT ────────────────────────────────────────────────────────────

class TokenManager:
    """Acquires and refreshes the X-LVS-HSToken."""

    def __init__(self):
        self._token: str | None = None
        self._expiry: float = 0

    @property
    def token(self) -> str | None:
        return self._token

    async def fetch(self, session: aiohttp.ClientSession) -> str:
        """Fetch a fresh token from /lvs-api/acc/token."""
        url = LVS_TOKEN_URL
        headers = {**HEADERS_TEMPLATE, "referer": f"{BASE_URL}/paris-tennis"}
        async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            data = await resp.json()
            self._token = data.get("hsToken")
            if not self._token:
                raise RuntimeError(f"Failed to get token: {data}")
            self._expiry = time.time() + 1800  # tokens last ~30 min
            logger.info("Token acquired")
            return self._token

    def is_expired(self) -> bool:
        return time.time() > self._expiry - 60  # refresh 1 min before expiry

    def set_token(self, token: str) -> None:
        self._token = token
        self._expiry = time.time() + 1800


# ── EVENT LISTING ───────────────────────────────────────────────────────────────

async def fetch_event_listing(
    session: aiohttp.ClientSession,
    token: str,
    path_id: str = TENNIS_PATH_ID,
    page_index: int = 0,
) -> dict:
    """
    Fetch tennis events from the lvs-api listing endpoint.
    Returns events (e<N>) without odds — lightweight, good for discovery.
    """
    url = f"{EVENT_LISTING_URL}/{path_id}"
    params = {
        "lineId": "1",
        "originId": "3",
        "breakdownEventsIntoDays": "true",
        "showPromotions": "true",
        "pageIndex": str(page_index),
    }
    headers = {**HEADERS_TEMPLATE, "x-lvs-hstoken": token}
    async with session.get(url, headers=headers, params=params,
                           timeout=aiohttp.ClientTimeout(total=15)) as resp:
        data = await resp.json()
        return data


async def fetch_all_tennis_events(
    session: aiohttp.ClientSession,
    token: str,
) -> list[dict]:
    """
    Paginate through all tennis events.
    Returns a flat list of event dicts (eType=G only, i.e. actual matches).
    """
    all_events: list[dict] = []
    seen_ids: set[str] = set()
    page = 0

    while True:
        data = await fetch_event_listing(session, token, TENNIS_PATH_ID, page)
        items = data.get("items", {})
        if not items:
            break

        new_count = 0
        for key, val in items.items():
            if key.startswith("e") and key not in seen_ids:
                seen_ids.add(key)
                if val.get("eType") == "G":  # G = game/match
                    all_events.append({"event_id": key, **val})

        # Check if we got less than a full page
        event_count = sum(1 for k in items if k.startswith("e"))
        if event_count < 20:
            break

        page += 1
        if page > 5:  # safety limit
            break

    logger.info(f"Discovered {len(all_events)} tennis events across {page + 1} pages")
    return all_events


# ── FOOTBALL-SPECIFIC ENDPOINTS ─────────────────────────────────────────────────

async def fetch_outcomes_percents(
    session: aiohttp.ClientSession,
    outcome_ids: list[int],
) -> dict[str, dict[str, int]]:
    """
    POST /service-sport-enligne-bff/v1/outcomes-percents

    Returns betting probability percentages per outcome, keyed by event_id → {outcome_id: pct}.
    Useful for showing implied probability alongside decimal odds.

    Example response: {"181236548": {"665236491": 95, "665236492": 3, "665236493": 2}}
    """
    headers = {
        **HEADERS_TEMPLATE,
        "content-type": "application/json",
        "referer": f"{BASE_URL}/paris-football",
    }
    async with session.post(
        OUTCOMES_PERCENTS_URL,
        headers=headers,
        json=outcome_ids,
        timeout=aiohttp.ClientTimeout(total=10),
    ) as resp:
        return await resp.json()


async def fetch_quick_access(session: aiohttp.ClientSession) -> list[dict]:
    """
    GET /service-sport-enligne-bff/v1/quick-access

    Returns popular competition groups with event IDs for each sport.
    Useful for discovering top football competitions without a token.
    """
    headers = {
        **HEADERS_TEMPLATE,
        "referer": f"{BASE_URL}/cotes-boostees",
    }
    async with session.get(
        QUICK_ACCESS_URL,
        headers=headers,
        timeout=aiohttp.ClientTimeout(total=10),
    ) as resp:
        return await resp.json()


# ── LIVE EVENTS + ODDS ──────────────────────────────────────────────────────────

async def fetch_live_events(
    session: aiohttp.ClientSession,
    token: str,
) -> dict:
    """
    Fetch live events with full market/outcome data.
    Returns {items, toBasket, numberOfEvents, marketTypeDisplayGroups}.
    Items dict keyed by l<N> (live event), m<N> (market), o<N> (outcome).
    """
    url = LIVE_EVENTS_URL
    params = {
        "lineId": "1",
        "originId": "3",
        "includeEventsWithNoClock": "true",
    }
    headers = {**HEADERS_TEMPLATE, "x-lvs-hstoken": token}
    async with session.get(url, headers=headers, params=params,
                           timeout=aiohttp.ClientTimeout(total=15)) as resp:
        data = await resp.json()
        return data


async def fetch_live_delta(
    session: aiohttp.ClientSession,
    token: str,
    from_basket: int,
) -> dict:
    """
    Fetch delta odds updates since a given basket number.
    Much lighter than full snapshot — use for polling live updates.
    """
    url = f"{LIVE_DELTA_URL}/{from_basket}"
    params = {
        "lineId": "1",
        "originId": "3",
        "includeEventsWithNoClock": "true",
    }
    headers = {**HEADERS_TEMPLATE, "x-lvs-hstoken": token}
    async with session.get(url, headers=headers, params=params,
                           timeout=aiohttp.ClientTimeout(total=15)) as resp:
        data = await resp.json()
        if "errors" in data:
            # Basket expired — need fresh snapshot
            return {"items": {}, "toBasket": from_basket, "expired": True}
        return data


# ── SSR MATCH DETAIL (prematch odds) ────────────────────────────────────────────

async def fetch_match_detail_ssr(
    session: aiohttp.ClientSession,
    event_id: int | str,
    slug: str | None = None,
) -> dict | None:
    """
    Scrape a match detail page for SSR state containing odds.
    The serverApp-state JSON blob has EventsDetail with groupedMarkets.

    Returns parsed events detail dict or None on failure.
    """
    eid = str(event_id)
    # Build URL — slug is optional (the server seems forgiving)
    if slug:
        url = f"{BASE_URL}/paris-tennis/atp/{slug}-m{eid}"
    else:
        url = f"{BASE_URL}/paris-tennis/atp/rome-h/{eid}/match"

    headers = {**HEADERS_TEMPLATE, "x-lvs-hstoken": ""}  # SSR doesn't need token
    try:
        async with session.get(url, headers=headers,
                               timeout=aiohttp.ClientTimeout(total=10)) as resp:
            html = await resp.text()

        # Extract serverApp-state JSON
        m = re.search(
            r'<script\s+id="serverApp-state"\s+type="application/json">(.+?)</script>',
            html, re.DOTALL,
        )
        if not m:
            logger.warning(f"No SSR state found for event {eid}")
            return None

        state = json.loads(m.group(1))
        events_detail = state.get("EventsDetail", {})
        return events_detail

    except Exception as exc:
        logger.error(f"SSR fetch failed for event {eid}: {exc}")
        return None


# ── ODDS EXTRACTION ─────────────────────────────────────────────────────────────

def parse_float_price(price_str: str | None) -> float:
    """Parse Unibet price format: '1,85' → 1.85, '60,00' → 60.0"""
    if not price_str:
        return 0.0
    return float(str(price_str).replace(",", "."))


def extract_face_a_face_odds(items: dict) -> dict[str, dict]:
    """
    Extract moneyline odds from a flat items dict for all tracked sports.

    Handles:
      - Tennis: markettypeId 68 "Face à Face" (2-way)
      - Football: markettypeId 1 "1 N 2" (3-way: home/draw/away)

    Returns {event_id: {
        "match": str, "competition": str, "live": bool,
        "odds": {"PlayerA": float, ...},
        "market_id": str, "score": {...},
    }}
    """
    result: dict[str, dict] = {}

    # 1. Index markets by parent event
    markets_by_event: dict[str, list[dict]] = {}
    outcomes_by_market: dict[str, list[dict]] = {}

    for key, val in items.items():
        if not isinstance(val, dict):
            continue
        if key.startswith("m") and val.get("markettypeId") in MONEYLINE_MARKET_TYPES:
            parent = val.get("parent", "")
            if parent:
                markets_by_event.setdefault(parent, []).append({"id": key, **val})
        elif key.startswith("o"):
            parent = val.get("parent", "")
            if parent:
                outcomes_by_market.setdefault(parent, []).append({"id": key, **val})

    # 2. Build result per event
    for event_key, event_val in items.items():
        if not isinstance(event_val, dict):
            continue
        if not (event_key.startswith("l") or event_key.startswith("e")):
            continue

        mkt_list = markets_by_event.get(event_key, [])
        if not mkt_list:
            continue

        for mkt in mkt_list:
            outcomes = outcomes_by_market.get(mkt["id"], [])
            if len(outcomes) < 2:
                continue

            odds = {}
            for o in sorted(outcomes, key=lambda x: x.get("pos", 0)):
                name = o.get("desc", f"Player{len(odds)+1}")
                price = o.get("price", "0")
                odds[name] = parse_float_price(price)

            # Build match info
            is_live = event_key.startswith("l")
            sport_code = event_val.get("code", "TENN")
            score = None
            if is_live:
                raw_score = event_val.get("score") or {}
                period = event_val.get("period") or {}
                time_val = event_val.get("time") or {}
                if sport_code == "FOOT":
                    score = {
                        "home":   raw_score.get("a"),
                        "away":   raw_score.get("b"),
                        "period": period.get("desc"),
                        "time_m": time_val.get("m"),
                        "time_s": time_val.get("s"),
                    }
                else:
                    score = {
                        "home": raw_score.get("a"),
                        "away": raw_score.get("b"),
                        "sets": event_val.get("set", []),
                        "match_score": event_val.get("match", []),
                        "current_set": event_val.get("currSet"),
                        "max_sets": event_val.get("max"),
                        "period": period.get("desc"),
                        "active": event_val.get("active"),
                        "time": time_val,
                    }

            evt_id = event_key.lstrip("le")
            result[event_key] = {
                "match_id":     event_key,
                "event_id":     int(evt_id) if evt_id.isdigit() else evt_id,
                "match":        event_val.get("desc", event_val.get("description", "")),
                "player_a":     event_val.get("a") or (event_val.get("opponentA") or {}).get("label", ""),
                "player_b":     event_val.get("b") or (event_val.get("opponentB") or {}).get("label", ""),
                "competition":  event_val.get("pdesc", ""),
                "start":        event_val.get("start", ""),
                "live":         is_live,
                "code":         event_val.get("code", "TENN"),
                "odds":         odds,
                "market_id":    mkt["id"],
                "score":        score,
                "parent":       event_val.get("parent", ""),
            }

    return result


def extract_ssr_odds(events_detail: dict) -> dict | None:
    """
    Extract Face à Face odds from SSR EventsDetail data.
    Returns same structure as extract_face_a_face_odds for a single event.
    """
    events = events_detail.get("events", [])
    if not events:
        return None

    ev = events[0]
    event_id = f"e{ev.get('id', '')}"
    grouped = ev.get("groupedMarkets", [])

    for group in grouped:
        for mkt in group.get("markets", []):
            if mkt.get("marketTypeId") == MARKET_FACE_A_FACE:
                outcomes = mkt.get("outcomes", [])
                if len(outcomes) < 2:
                    continue

                odds = {}
                for o in sorted(outcomes, key=lambda x: x.get("pos", 0)):
                    name = o.get("description", f"Player{len(odds)+1}")
                    price = o.get("price", "0")
                    odds[name] = parse_float_price(price)

                return {
                    "match_id":     event_id,
                    "event_id":     ev.get("id"),
                    "match":        f"{odds.get(list(odds.keys())[0] if odds else '?', '?')} vs {odds.get(list(odds.keys())[1] if len(odds) > 1 else '?', '?')}",
                    "player_a":     list(odds.keys())[0] if odds else "",
                    "player_b":     list(odds.keys())[1] if len(odds) > 1 else "",
                    "competition":  "",
                    "start":        ev.get("start", ""),
                    "live":         False,
                    "code":         ev.get("sportCode", "TENN"),
                    "odds":         odds,
                    "market_id":    str(mkt.get("id", "")),
                    "score":        None,
                    "parent":       ev.get("parent", ""),
                }

    return None


# ── MATCH URL BUILDER ───────────────────────────────────────────────────────────

def build_match_slug(event: dict) -> str:
    """Build a URL-friendly slug for a match."""
    a = event.get("a") or (event.get("opponentA") or {}).get("label", "")
    b = event.get("b") or (event.get("opponentB") or {}).get("label", "")
    if a and b:
        a_slug = a.lower().replace(" ", "-").replace(".", "").replace("'", "")
        b_slug = b.lower().replace(" ", "-").replace(".", "").replace("'", "")
        return f"{a_slug}-vs-{b_slug}"
    return ""


# ── LIVE STATE EXTRACTION ───────────────────────────────────────────────────────

def extract_live_state(event: dict) -> dict:
    """Extract live match state from a live event dict."""
    score = event.get("score", {})
    sets = event.get("set", [])
    period = event.get("period", {})

    return {
        "is_live":    True,
        "score_a":    score.get("a"),
        "score_b":    score.get("b"),
        "sets":       [{"a": s.get("a"), "b": s.get("b"), "win": s.get("win")} for s in sets],
        "current_set": event.get("currSet"),
        "max_sets":    event.get("max"),
        "period_desc": period.get("desc"),
        "active":      event.get("active"),
        "time_seconds": (event.get("time") or {}).get("m", 0) * 60 + (event.get("time") or {}).get("s", 0),
        "tiebreak":    event.get("tieBreak", False),
    }


# ── SSL / SESSION ────────────────────────────────────────────────────────────────

def create_connector(verify_ssl: bool = True, limit: int = 20, limit_per_host: int = 10) -> aiohttp.TCPConnector:
    """Create a TCPConnector with optional SSL verification bypass."""
    ssl_context = None
    if not verify_ssl:
        ssl_context = ssl.create_default_context()
        ssl_context.check_hostname = False
        ssl_context.verify_mode = ssl.CERT_NONE
    return aiohttp.TCPConnector(
        limit=limit,
        limit_per_host=limit_per_host,
        ttl_dns_cache=300,
        enable_cleanup_closed=True,
        force_close=False,
        ssl=ssl_context if not verify_ssl else True,
    )


# ── UTILITY ─────────────────────────────────────────────────────────────────────

def parse_start_time(start_str: str) -> datetime | None:
    """Parse Unibet start time format 'YYMMDDHHmm' → datetime."""
    if not start_str or len(start_str) < 10:
        return None
    try:
        return datetime.strptime(start_str[:10], "%y%m%d%H%M").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


if __name__ == "__main__":
    async def test():
        logging.basicConfig(level=logging.INFO)
        connector = create_connector(verify_ssl=False)
        async with aiohttp.ClientSession(connector=connector) as session:
            tm = TokenManager()
            token = await tm.fetch(session)

            print("\n=== Live Events ===")
            live = await fetch_live_events(session, token)
            items = live.get("items", {})
            print(f"Items: {len(items)}, toBasket: {live.get('toBasket')}")

            tennis_odds = extract_face_a_face_odds(items)
            for mid, data in tennis_odds.items():
                print(f"  {mid}: {data['match']} | {data['odds']} | live={data['live']}")

            print(f"\n=== Event Listing (first page) ===")
            listing = await fetch_event_listing(session, token, TENNIS_PATH_ID)
            litems = listing.get("items", {})
            for k in sorted(litems.keys())[:10]:
                v = litems[k]
                print(f"  {k} ({k[0]}): {v.get('desc', v.get('description', '?'))[:80]}")

    asyncio.run(test())
