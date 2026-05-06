"""Unibet Tennis Odds Feed — async poll scheduler."""

import asyncio
import logging
import time

import aiohttp

from api.client import (
    HEADERS_TEMPLATE,
    TENNIS_PATH_ID,
    create_connector,
    fetch_live_events,
    fetch_live_delta,
    fetch_event_listing,
)
from api.token import TokenManager
from config import settings
from feed.circuit_breaker import CircuitBreaker
from feed.parser import extract_face_a_face_odds, extract_live_state

logger = logging.getLogger("unibet_feed")

# ── SHARED STATE ──────────────────────────────────────────────────────────────────
_meta: dict = {}
_stats: dict = {
    "streams":     0,
    "updates":     0,
    "last_update": None,
    "matches":     0,
    "live_matches": 0,
    "prematch_matches": 0,
    "errors":      0,
}
_stop_event: asyncio.Event | None = None
_poll_interval = settings.poll_interval
_max_match_age_h = settings.max_match_age_h


# ── PUBLIC CONFIG HELPERS ─────────────────────────────────────────────────────────

def set_poll_interval(seconds: float) -> None:
    global _poll_interval
    _poll_interval = seconds

def set_max_age(hours: float) -> None:
    global _max_match_age_h
    _max_match_age_h = hours

def get_stats() -> dict:
    return dict(_stats)

def get_meta(match_id: str) -> dict:
    return dict(_meta.get(match_id, {}))

def get_all_meta() -> dict:
    return dict(_meta)

def stop() -> None:
    if _stop_event is not None:
        _stop_event.set()


# ── ODDS DIFF ENGINE ──────────────────────────────────────────────────────────────

def _odds_changed(old: dict, new: dict) -> dict | None:
    """Return changed odds entries, or None if nothing changed."""
    changed = {}
    for sel, odd in new.items():
        if sel not in old or abs(old[sel] - odd) > 0.001:
            changed[sel] = odd
    return changed or None


# ── MATCH LIST REFRESH ────────────────────────────────────────────────────────────

async def _refresh_match_list(
    session: aiohttp.ClientSession,
    token: str,
    queue: asyncio.Queue,
    prematch_fetched: set[str],
) -> None:
    """Fetch full tennis event listing with odds from lvs-api."""
    try:
        all_pages_data = []
        page = 0
        seen_event_ids: set[str] = set()

        while page < 5:
            data = await fetch_event_listing(session, token, TENNIS_PATH_ID, page)
            items = data.get("items", {})
            if not items:
                break

            new_events = [k for k in items if k.startswith("e")]
            new_ids = set(new_events)
            if not new_ids or new_ids.issubset(seen_event_ids):
                break

            seen_event_ids.update(new_ids)
            all_pages_data.append(data)

            if len(new_events) < 20:
                break
            page += 1

        total_odds = 0
        for data in all_pages_data:
            items = data.get("items", {})
            odds = extract_face_a_face_odds(items)

            for match_id, match_data in odds.items():
                odds_dict = match_data.get("odds", {})
                if len(odds_dict) < 2:
                    continue

                meta = {
                    "match":       match_data.get("match", ""),
                    "player_a":    match_data.get("player_a", ""),
                    "player_b":    match_data.get("player_b", ""),
                    "competition": match_data.get("competition", ""),
                    "date":        match_data.get("start", ""),
                    "live":        match_data.get("live", False),
                    "code":        match_data.get("code", "TENN"),
                }
                _meta[match_id] = meta

                update = {
                    "source":     "unibet",
                    "match_id":   match_id,
                    "market":     "1X2",
                    "odds":       odds_dict,
                    "meta":       meta,
                    "live":       meta["live"],
                    "score":      match_data.get("score"),
                    "ts":         time.time(),
                }
                try:
                    queue.put_nowait(update)
                    total_odds += 1
                except asyncio.QueueFull:
                    pass

        if total_odds:
            _stats["updates"] += total_odds
            _stats["prematch_matches"] = total_odds
            _stats["last_update"] = time.time()
            logger.info(f"Match list refresh: {total_odds} tennis matches with odds")

    except Exception as exc:
        logger.error(f"Match list refresh error: {exc}")


# ── MAIN FEED COROUTINE ───────────────────────────────────────────────────────────

async def run(queue: asyncio.Queue, poll_interval: float | None = None) -> None:
    """Main feed coroutine. Polls live API for odds and pushes updates to queue."""
    global _stop_event, _poll_interval
    _stop_event = asyncio.Event()

    if poll_interval is not None:
        _poll_interval = poll_interval

    interval = _poll_interval
    breaker = CircuitBreaker(
        threshold=settings.circuit_breaker_threshold,
        cooldown=settings.circuit_breaker_cooldown,
    )

    connector = create_connector(
        verify_ssl=settings.ssl_verify,
        limit=settings.connection_pool_limit,
        limit_per_host=settings.connection_pool_per_host,
    )
    async with aiohttp.ClientSession(
        headers=dict(HEADERS_TEMPLATE),
        connector=connector,
        connector_owner=True,
    ) as session:

        tm = TokenManager()
        last_basket: int = 0
        last_odds: dict[str, dict[str, float]] = {}
        last_match_refresh: float = 0
        prematch_odds_fetched: set[str] = set()

        while not _stop_event.is_set():

            if breaker.is_open:
                await asyncio.sleep(1)
                continue

            try:
                if tm.is_expired() or tm.token is None:
                    await tm.fetch(session)

                token = tm.token

                now = time.time()
                if now - last_match_refresh > settings.match_refresh_min * 60:
                    await _refresh_match_list(session, token, queue, prematch_odds_fetched)
                    last_match_refresh = now

                if last_basket > 0:
                    data = await fetch_live_delta(session, token, last_basket)
                    if data.get("expired"):
                        data = await fetch_live_events(session, token)
                else:
                    data = await fetch_live_events(session, token)

                if "errors" in data:
                    raise RuntimeError(f"API error: {data['errors']}")

                items = data.get("items", {})
                new_basket = data.get("toBasket", last_basket)

                if new_basket > last_basket:
                    last_basket = new_basket

                all_odds = extract_face_a_face_odds(items)
                updates_pushed = 0

                for match_id, match_data in all_odds.items():
                    odds = match_data.get("odds", {})

                    score_data = extract_live_state(match_data) if match_data.get("live") else {}
                    meta = {
                        "match":       match_data.get("match", ""),
                        "player_a":    match_data.get("player_a", ""),
                        "player_b":    match_data.get("player_b", ""),
                        "competition": match_data.get("competition", ""),
                        "date":        match_data.get("start", ""),
                        "live":        match_data.get("live", False),
                        "code":        match_data.get("code", "TENN"),
                        **score_data,
                    }
                    _meta[match_id] = meta

                    prev = last_odds.get(match_id, {})
                    changed = _odds_changed(prev, odds)

                    if changed or match_id not in last_odds:
                        update = {
                            "source":     "unibet",
                            "match_id":   match_id,
                            "market":     "1X2",
                            "odds":       odds,
                            "meta":       meta,
                            "live":       meta["live"],
                            "score":      match_data.get("score"),
                            "ts":         time.time(),
                        }

                        try:
                            queue.put_nowait(update)
                            updates_pushed += 1
                        except asyncio.QueueFull:
                            pass

                        last_odds[match_id] = dict(odds)

                if updates_pushed:
                    _stats["updates"] += updates_pushed
                    _stats["last_update"] = time.time()
                    _stats["matches"] = len(all_odds)
                    _stats["live_matches"] = sum(1 for m in all_odds.values() if m.get("live"))

                breaker.record_success()

            except Exception as exc:
                breaker.record_failure()
                _stats["errors"] += 1
                logger.error(f"Feed error: {type(exc).__name__}: {exc}")

            try:
                await asyncio.wait_for(_stop_event.wait(), timeout=interval)
                break
            except asyncio.TimeoutError:
                pass

        logger.info("Feed shut down")
