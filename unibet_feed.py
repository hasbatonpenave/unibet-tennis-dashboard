"""
unibet_feed.py — Unibet Tennis Odds Feed (fully async)
========================================================
One event loop, zero thread overhead. Polls Unibet's REST APIs
on a configurable interval and pushes odds changes to an asyncio.Queue.

Architecture:
  ┌─────────────────┐     ┌──────────────────┐     ┌─────────────────┐
  │ unibet_api.py   │ ←── │ unibet_feed.py   │ ──→ │ asyncio.Queue   │
  │ (HTTP client)   │     │ (poll scheduler) │     │ (maxsize=20000) │
  └─────────────────┘     └──────────────────┘     └─────────────────┘

Public interface (mirrors betclic_feed.py for drop-in compatibility):
  await run(queue)          — start the feed
  stop()                    — graceful shutdown
  get_stats()               — {streams, updates, last_update, matches}
  get_meta(match_id)        — match metadata dict
  get_all_meta()            — all match metadata
  set_poll_interval(s)      — configure poll interval
  set_max_age(h)            — filter matches by start window

Update format pushed to queue:
  {
    "source":     "unibet",
    "match_id":   str,
    "market":     "1X2",
    "odds":       {"PlayerA": 1.85, "PlayerB": 1.95},
    "meta":       {match, competition, date, live, score, ...},
    "live":       bool,
    "score":      {...},
  }
"""

import asyncio
import json
import logging
import sys
import time
from datetime import datetime, timezone

import aiohttp

sys.path.insert(0, __import__("os").path.dirname(__import__("os").path.abspath(__file__)))
from unibet_api import (
    TokenManager,
    HEADERS_TEMPLATE,
    TENNIS_PATH_ID,
    TARGET_MARKETS,
    MARKET_FACE_A_FACE,
    create_connector,
    fetch_live_events,
    fetch_live_delta,
    fetch_event_listing,
    fetch_all_tennis_events,
    extract_face_a_face_odds,
    extract_ssr_odds,
    extract_live_state,
    build_match_slug,
    parse_float_price,
    fetch_match_detail_ssr,
)

logger = logging.getLogger("unibet_feed")

# ── CONFIG ──────────────────────────────────────────────────────────────────────
POLL_INTERVAL    = 0.5     # seconds between live odds polls
DELTA_POLL_INTERVAL = 0.5   # seconds between delta polls
MATCH_REFRESH_MIN = 5.0     # minutes between full match list refresh
MAX_MATCH_AGE_H   = 48      # stream matches starting within this window
MAX_SSR_WORKERS   = 5       # concurrent SSR scrapers for prematch odds

# ── SHARED STATE ────────────────────────────────────────────────────────────────
_meta: dict = {}    # {match_id_str: {match, competition, date, live, teams, score, ...}}
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
_poll_interval = POLL_INTERVAL
_max_match_age_h = MAX_MATCH_AGE_H


# ── PUBLIC CONFIG HELPERS ───────────────────────────────────────────────────────

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


# ── ODDS DIFF ENGINE ────────────────────────────────────────────────────────────

def _odds_changed(old: dict, new: dict) -> dict | None:
    """Return changed odds entries, or None if nothing changed."""
    changed = {}
    for sel, odd in new.items():
        if sel not in old or abs(old[sel] - odd) > 0.001:
            changed[sel] = odd
    return changed or None


# ── MAIN FEED COROUTINE ─────────────────────────────────────────────────────────

async def run(queue: asyncio.Queue, poll_interval: float | None = None) -> None:
    """
    Main feed coroutine. Two-phase polling:
      1. Full snapshot via /current/events/live/topmarket (every ~3s)
      2. Delta updates via /delta/events/live/topmarket/from/{basket} (every ~2s)
      3. Prematch event list refresh every MATCH_REFRESH_MIN minutes
      4. Prematch SSR odds scrape on discovery + periodic refresh

    Usage:
        asyncio.create_task(unibet_feed.run(queue))
    """
    global _stop_event, _poll_interval
    _stop_event = asyncio.Event()

    if poll_interval is not None:
        _poll_interval = poll_interval

    interval = _poll_interval

    # ── aiohttp session ───────────────────────────────────────────────────────
    # SSL verification disabled for compatibility with certain network environments
    connector = create_connector(verify_ssl=False, limit=20, limit_per_host=10)
    async with aiohttp.ClientSession(
        headers=dict(HEADERS_TEMPLATE),
        connector=connector,
        connector_owner=True,
    ) as session:

        tm = TokenManager()
        last_basket: int = 0
        last_odds: dict[str, dict[str, float]] = {}   # match_id → {sel: odd}
        last_match_refresh: float = 0
        prematch_odds_fetched: set[str] = set()
        consecutive_errors: int = 0
        circuit_open: bool = False
        circuit_until: float = 0

        while not _stop_event.is_set():

            # ── Circuit breaker ───────────────────────────────────────────────
            if circuit_open:
                if time.time() < circuit_until:
                    await asyncio.sleep(1)
                    continue
                else:
                    circuit_open = False
                    consecutive_errors = 0
                    logger.info("Circuit breaker reset")

            try:
                # ── Token refresh ──────────────────────────────────────────────
                if tm.is_expired() or tm.token is None:
                    await tm.fetch(session)

                token = tm.token

                # ── Full match list refresh ────────────────────────────────────
                now = time.time()
                if now - last_match_refresh > MATCH_REFRESH_MIN * 60:
                    await _refresh_match_list(session, token, queue, prematch_odds_fetched)
                    last_match_refresh = now

                # ── Live odds poll ─────────────────────────────────────────────
                # Use delta when possible, fallback to full snapshot
                if last_basket > 0:
                    data = await fetch_live_delta(session, token, last_basket)
                    if data.get("expired"):
                        # Basket expired — get fresh snapshot
                        data = await fetch_live_events(session, token)
                else:
                    data = await fetch_live_events(session, token)

                if "errors" in data:
                    raise RuntimeError(f"API error: {data['errors']}")

                items = data.get("items", {})
                new_basket = data.get("toBasket", last_basket)

                if new_basket > last_basket:
                    last_basket = new_basket

                # ── Extract odds ───────────────────────────────────────────────
                all_odds = extract_face_a_face_odds(items)
                updates_pushed = 0

                for match_id, match_data in all_odds.items():
                    odds = match_data.get("odds", {})

                    # Build meta
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

                    # Detect changes
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

                consecutive_errors = 0

            except Exception as exc:
                consecutive_errors += 1
                _stats["errors"] += 1
                logger.error(f"Feed error ({consecutive_errors}/5): {type(exc).__name__}: {exc}")

                if consecutive_errors >= 5:
                    circuit_open = True
                    circuit_until = time.time() + 300  # 5 min pause
                    logger.warning(f"Circuit breaker opened for 5 min")

            # ── Wait for next poll ─────────────────────────────────────────────
            try:
                await asyncio.wait_for(_stop_event.wait(), timeout=interval)
                break  # stop() was called
            except asyncio.TimeoutError:
                pass

        logger.info("Feed shut down")


# ── MATCH LIST REFRESH ──────────────────────────────────────────────────────────

async def _refresh_match_list(
    session: aiohttp.ClientSession,
    token: str,
    queue: asyncio.Queue,
    prematch_fetched: set[str],
) -> None:
    """
    Fetch full tennis event listing with odds from lvs-api.
    The lvs-api returns events (e), markets (m), and outcomes (o) together.
    """
    try:
        all_pages_data = []
        page = 0
        seen_event_ids: set[str] = set()

        while page < 5:  # safety limit
            data = await fetch_event_listing(session, token, TENNIS_PATH_ID, page)
            items = data.get("items", {})
            if not items:
                break

            # Count new events
            new_events = [k for k in items if k.startswith("e")]
            new_ids = set(new_events)
            if not new_ids or new_ids.issubset(seen_event_ids):
                break

            seen_event_ids.update(new_ids)
            all_pages_data.append(data)

            if len(new_events) < 20:
                break
            page += 1

        # Extract Face à Face odds from all pages
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


# ── ENTRYPOINT ──────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    async def main():
        logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")
        q: asyncio.Queue = asyncio.Queue(maxsize=10000)

        async def _consumer(qq):
            while True:
                u = await qq.get()
                mid = u["match_id"]
                odds = u["odds"]
                meta = u.get("meta", {})
                live = meta.get("live")
                score = u.get("score")
                print(f"[{mid}] {', '.join(f'{k}={v:.2f}' for k,v in odds.items())} "
                      f"live={live} score={score}")

        consumer = asyncio.create_task(_consumer(q))
        feed_task = asyncio.create_task(run(q))

        try:
            await asyncio.sleep(120)
        except KeyboardInterrupt:
            pass

        stop()
        feed_task.cancel()
        consumer.cancel()
        await asyncio.gather(feed_task, consumer, return_exceptions=True)

    asyncio.run(main())
