"""Unibet.fr Sports API HTTP client functions."""

import asyncio
import json
import logging
import re
import ssl
import time
from datetime import datetime, timezone
from typing import Any

import aiohttp

from config import settings

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

# ── SOCCER PATH IDs ───────────────────────────────────────────────────────────────
SOCCER_PATH_IDS = settings.soccer_path_ids

# ── TENNIS MARKET TYPE IDs ────────────────────────────────────────────────────────
MARKET_FACE_A_FACE     = 68
MARKET_SET_WINNER      = 69
MARKET_TOTAL_GAMES     = 361
MARKET_EXACT_SCORE     = 840
MARKET_GAME_HANDICAP   = 130010
MARKET_SET_BETTING     = 10098
MARKET_WIN_ONE_SET     = 120997

# ── SOCCER MARKET TYPE IDs ────────────────────────────────────────────────────────
MARKET_1X2            = 1
MARKET_OVER_UNDER_25  = 18
MARKET_BTTS           = 24

# ── TARGET MARKETS BY SPORT ───────────────────────────────────────────────────────
TARGET_MARKETS: dict[str, dict[int, str]] = {}

def _build_target_markets() -> dict[str, dict[int, str]]:
    """Build TARGET_MARKETS dict from settings."""
    result: dict[str, dict[int, str]] = {}
    if settings.tennis_enabled:
        result["TENN"] = dict(settings.tennis_market_ids)
    if settings.soccer_enabled:
        result["FOOT"] = dict(settings.soccer_market_ids)
    return result


def refresh_target_markets() -> None:
    """Refresh TARGET_MARKETS from current settings (call after config changes)."""
    global TARGET_MARKETS
    TARGET_MARKETS = _build_target_markets()


# Initialize at import time
refresh_target_markets()

# ── SPORT PATH IDs ────────────────────────────────────────────────────────────────
def get_sport_path_ids() -> dict[str, list[str]]:
    """Return {sport_code: [path_ids]} for enabled sports."""
    result: dict[str, list[str]] = {}
    if settings.tennis_enabled:
        result["TENN"] = list(settings.tennis_path_ids)
    if settings.soccer_enabled:
        result["FOOT"] = list(settings.soccer_path_ids)
    return result


# ── HTTP HEADERS ──────────────────────────────────────────────────────────────────
HEADERS_TEMPLATE = {
    "accept":             "application/json, text/plain, */*",
    "accept-language":    "fr,en-US;q=0.9,en;q=0.8",
    "dnt":                "1",
    "origin":             BASE_URL,
    "referer":            f"{BASE_URL}/paris-sport",  # neutral — works for all sports
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
    """Fetch events from the lvs-api listing endpoint for a given path."""
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
    pages = await fetch_all_sport_pages(session, token, ["TENN"])
    all_events = []
    seen = set()
    for data in pages:
        for key, val in data.get("items", {}).items():
            if key.startswith("e") and key not in seen and val.get("eType") == "G":
                seen.add(key)
                all_events.append({"event_id": key, **val})
    return all_events


async def fetch_all_sport_pages(
    session: aiohttp.ClientSession,
    token: str,
    sport_codes: list[str] | None = None,
) -> list[dict]:
    """Paginate through event listings. Returns list of raw API page responses."""
    if sport_codes is None:
        sport_codes = list(get_sport_path_ids().keys())

    sport_paths = get_sport_path_ids()
    all_pages: list[dict] = []

    for code in sport_codes:
        path_ids = sport_paths.get(code, [])
        for pid in path_ids:
            page = 0
            while page < 5:
                try:
                    data = await fetch_event_listing(session, token, pid, page)
                except Exception as exc:
                    logger.warning(f"Failed to fetch {code} path {pid} page {page}: {exc}")
                    break

                items = data.get("items", {})
                if not items:
                    break

                all_pages.append(data)

                event_count = sum(1 for k in items if k.startswith("e"))
                if event_count < 20:
                    break
                page += 1

    logger.info(f"Discovered {len(all_pages)} pages across sports {sport_codes}")
    return all_pages


async def fetch_all_sport_events(
    session: aiohttp.ClientSession,
    token: str,
    sport_codes: list[str] | None = None,
) -> list[dict]:
    """Paginate through events. Returns flat list of event dicts (backward compat)."""
    if sport_codes is None:
        sport_codes = list(get_sport_path_ids().keys())

    pages = await fetch_all_sport_pages(session, token, sport_codes)
    all_events: list[dict] = []
    seen: set[str] = set()

    for data in pages:
        for key, val in data.get("items", {}).items():
            if key.startswith("e") and key not in seen and val.get("eType") == "G":
                seen.add(key)
                all_events.append({"event_id": key, **val})

    logger.info(f"Discovered {len(all_events)} events across sports {sport_codes}")
    return all_events


# ── SSR MATCH DETAIL ──────────────────────────────────────────────────────────────

async def fetch_match_detail_ssr(
    session: aiohttp.ClientSession,
    event_id: int | str,
    slug: str | None = None,
    sport: str = "tennis",
) -> dict | None:
    """Scrape a match detail page for SSR state containing odds."""
    eid = str(event_id)
    sport_path = "paris-football" if sport == "soccer" else "paris-tennis"
    if slug:
        url = f"{BASE_URL}/{sport_path}/atp/{slug}-m{eid}"
    else:
        url = f"{BASE_URL}/{sport_path}/atp/rome-h/{eid}/match"

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
