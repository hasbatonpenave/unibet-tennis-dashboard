"""Unibet.fr Tennis API HTTP client functions."""

import asyncio
import json
import logging
import re
import ssl
import time
from datetime import datetime, timezone
from typing import Any

import aiohttp

logger = logging.getLogger("unibet_api")

# ── URL CONSTANTS ─────────────────────────────────────────────────────────────────
BASE_URL = "https://www.unibet.fr"
LVS_TOKEN_URL = f"{BASE_URL}/lvs-api/acc/token"
LIVE_EVENTS_URL = f"{BASE_URL}/services-api/sportsbookdata/current/events/live/topmarket"
LIVE_DELTA_URL = f"{BASE_URL}/services-api/sportsbookdata/delta/events/live/topmarket/from"
EVENT_LISTING_URL = f"{BASE_URL}/lvs-api/next/50"

# ── TENNIS PATH IDs ───────────────────────────────────────────────────────────────
TENNIS_PATH_ID = "p239"
ATP_PATH_ID    = "p58484924"
WTA_PATH_ID    = "p58484929"

# ── TENNIS MARKET TYPE IDs ────────────────────────────────────────────────────────
MARKET_FACE_A_FACE     = 68
MARKET_SET_WINNER      = 69
MARKET_TOTAL_GAMES     = 361
MARKET_EXACT_SCORE     = 840
MARKET_GAME_HANDICAP   = 130010
MARKET_SET_BETTING     = 10098
MARKET_WIN_ONE_SET     = 120997

TARGET_MARKETS = {
    MARKET_FACE_A_FACE: "Face à Face",
}

# ── HTTP HEADERS ──────────────────────────────────────────────────────────────────
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


# ── SSL / SESSION ─────────────────────────────────────────────────────────────────

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


# ── LIVE EVENTS + ODDS ────────────────────────────────────────────────────────────

async def fetch_live_events(
    session: aiohttp.ClientSession,
    token: str,
) -> dict:
    """Fetch live events with full market/outcome data."""
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
    """Fetch delta odds updates since a given basket number."""
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
            return {"items": {}, "toBasket": from_basket, "expired": True}
        return data


# ── EVENT LISTING ─────────────────────────────────────────────────────────────────

async def fetch_event_listing(
    session: aiohttp.ClientSession,
    token: str,
    path_id: str = TENNIS_PATH_ID,
    page_index: int = 0,
) -> dict:
    """Fetch tennis events from the lvs-api listing endpoint."""
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
    """Paginate through all tennis events. Returns flat list of event dicts."""
    all_events: list[dict] = []
    seen_ids: set[str] = set()
    page = 0

    while True:
        data = await fetch_event_listing(session, token, TENNIS_PATH_ID, page)
        items = data.get("items", {})
        if not items:
            break

        for key, val in items.items():
            if key.startswith("e") and key not in seen_ids:
                seen_ids.add(key)
                if val.get("eType") == "G":
                    all_events.append({"event_id": key, **val})

        event_count = sum(1 for k in items if k.startswith("e"))
        if event_count < 20:
            break

        page += 1
        if page > 5:
            break

    logger.info(f"Discovered {len(all_events)} tennis events across {page + 1} pages")
    return all_events


# ── SSR MATCH DETAIL ──────────────────────────────────────────────────────────────

async def fetch_match_detail_ssr(
    session: aiohttp.ClientSession,
    event_id: int | str,
    slug: str | None = None,
) -> dict | None:
    """Scrape a match detail page for SSR state containing odds."""
    eid = str(event_id)
    if slug:
        url = f"{BASE_URL}/paris-tennis/atp/{slug}-m{eid}"
    else:
        url = f"{BASE_URL}/paris-tennis/atp/rome-h/{eid}/match"

    headers = {**HEADERS_TEMPLATE, "x-lvs-hstoken": ""}
    try:
        async with session.get(url, headers=headers,
                               timeout=aiohttp.ClientTimeout(total=10)) as resp:
            html = await resp.text()

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
