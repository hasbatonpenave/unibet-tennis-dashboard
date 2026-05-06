"""Server application — FastAPI, SSE streaming, REST endpoints."""

import asyncio
import json
import logging
import threading
import time
from contextlib import asynccontextmanager
from typing import AsyncGenerator

from fastapi import FastAPI, Query
from fastapi.responses import StreamingResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware

from config import settings
from feed import manager as feed_manager
from server.broadcaster import Broadcaster
from storage.sqlite import (
    _sqlite_writer,
    close_read_connection,
    get_db_queue,
    open_read_connection,
    query_history,
)

logger = logging.getLogger("unibet_server")

# ── IN-MEMORY STATE ───────────────────────────────────────────────────────────────
prices: dict[str, dict[str, dict[str, float]]] = {}
prices_lock = asyncio.Lock()

_price_previous: dict[str, dict[str, dict[str, float]]] = {}
_price_movements: dict[str, dict[str, dict[str, str]]] = {}

broadcaster = Broadcaster()


# ── FEED CONSUMER ─────────────────────────────────────────────────────────────────

def _detect_movement(match_id: str, market: str, odds: dict) -> dict:
    """Compare new odds with previous to detect direction."""
    movements = {}
    prev = _price_previous.get(match_id, {}).get(market, {})
    for sel, price in odds.items():
        if sel in prev:
            if price > prev[sel] + 0.001:
                movements[sel] = "up"
            elif price < prev[sel] - 0.001:
                movements[sel] = "down"
            else:
                movements[sel] = "steady"
        else:
            movements[sel] = "new"

    _price_previous.setdefault(match_id, {}).setdefault(market, {}).update(odds)
    _price_movements.setdefault(match_id, {}).setdefault(market, {}).update(movements)
    return movements


async def consume_feed(q: asyncio.Queue):
    """Reads updates from feed, fans out to SSE clients, writes to SQLite."""
    db_queue = get_db_queue()

    while True:
        update: dict = await q.get()

        match_id = update["match_id"]
        market   = update["market"]
        odds     = update["odds"]
        meta     = update.get("meta", {})
        ts       = update.get("ts", time.time())

        async with prices_lock:
            mdata = prices.setdefault(match_id, {})
            mdata.setdefault(market, {}).update(odds)

        movements = _detect_movement(match_id, market, odds)

        match_name  = meta.get("match", "")
        player_a    = meta.get("player_a", "")
        player_b    = meta.get("player_b", "")
        competition = meta.get("competition", "")
        match_date  = meta.get("date", "")
        is_live     = int(meta.get("live", False))

        for selection, odd in odds.items():
            db_queue.put_nowait((
                ts, match_id, market, selection, odd,
                match_name, player_a, player_b, competition, match_date, is_live,
            ))

        event_json = json.dumps({
            "type":        "price",
            "match_id":    match_id,
            "market":      market,
            "odds":        odds,
            "movements":   movements,
            "meta":        meta,
            "score":       update.get("score"),
            "live":        update.get("live", False),
            "ts":          ts,
        }, ensure_ascii=False)

        broadcaster.broadcast(event_json)


# ── LIFESPAN ──────────────────────────────────────────────────────────────────────
_feed_queue: asyncio.Queue | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _feed_queue

    open_read_connection(settings.db_path)

    db_thread = threading.Thread(
        target=_sqlite_writer,
        args=(settings.db_flush_interval, settings.db_batch_size),
        daemon=True,
        name="unibet-db-writer",
    )
    db_thread.start()
    logger.info("SQLite writer started")

    _feed_queue = asyncio.Queue(maxsize=settings.feed_queue_maxsize)
    feed_task     = asyncio.create_task(feed_manager.run(_feed_queue), name="unibet-feed")
    consumer_task = asyncio.create_task(consume_feed(_feed_queue), name="unibet-consumer")
    logger.info(f"Feed and consumer running on port {settings.port}")

    yield

    feed_manager.stop()
    feed_task.cancel()
    consumer_task.cancel()
    get_db_queue().put(None)
    db_thread.join(timeout=10)
    close_read_connection()
    logger.info("Shutdown complete")


# ── APP ───────────────────────────────────────────────────────────────────────────
app = FastAPI(title="Unibet Tennis Feed", version="1.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── SSE /stream ───────────────────────────────────────────────────────────────────

@app.get("/stream")
async def stream_sse():
    """Server-Sent Events endpoint. Sends initial snapshot then live updates."""
    q = broadcaster.subscribe()

    async with prices_lock:
        snapshot = dict(prices)

    snapshot_json = json.dumps({
        "type":   "snapshot",
        "prices": snapshot,
        "meta":   feed_manager.get_all_meta(),
        "ts":     time.time(),
    }, ensure_ascii=False)

    async def generator() -> AsyncGenerator[str, None]:
        try:
            yield f"data: {snapshot_json}\n\n"
            while True:
                try:
                    data = await asyncio.wait_for(q.get(), timeout=25.0)
                    yield f"data: {data}\n\n"
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        except (asyncio.CancelledError, GeneratorExit):
            pass
        finally:
            broadcaster.unsubscribe(q)

    return StreamingResponse(
        generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control":     "no-cache",
            "X-Accel-Buffering": "no",
            "Connection":        "keep-alive",
        },
    )


# ── REST ENDPOINTS ────────────────────────────────────────────────────────────────

@app.get("/prices")
async def get_prices():
    """Full in-memory odds snapshot with movements."""
    async with prices_lock:
        p = dict(prices)
        movements = dict(_price_movements)
    return JSONResponse({
        "prices":    p,
        "movements": movements,
        "ts":        time.time(),
    })


@app.get("/markets")
async def get_markets():
    """Active matches with full metadata."""
    all_meta = feed_manager.get_all_meta()
    async with prices_lock:
        p = dict(prices)
        movements = dict(_price_movements)
    result = {}
    for mid in p:
        meta = all_meta.get(mid, {})
        result[mid] = {
            **meta,
            "odds": p.get(mid, {}),
            "movements": movements.get(mid, {}),
        }
    return JSONResponse(result)


@app.get("/status")
async def get_status():
    """Feed health: stream count, update count, SSE clients."""
    stats = feed_manager.get_stats()
    stats["sse_clients"]      = broadcaster.client_count
    stats["prices_in_memory"] = len(prices)
    stats["db_queue_size"]    = get_db_queue().qsize()
    return JSONResponse(stats)


@app.get("/live-scores")
async def get_live_scores():
    """Current scores for live tennis matches only."""
    all_meta = feed_manager.get_all_meta()
    async with prices_lock:
        p = dict(prices)
    live_matches = {}
    for mid, meta in all_meta.items():
        if meta.get("live"):
            live_matches[mid] = {
                "match":       meta.get("match", ""),
                "competition": meta.get("competition", ""),
                "score_a":     meta.get("score_a"),
                "score_b":     meta.get("score_b"),
                "sets":        meta.get("sets"),
                "current_set": meta.get("current_set"),
                "period_desc": meta.get("period_desc"),
                "active":      meta.get("active"),
                "tiebreak":    meta.get("tiebreak"),
                "time_seconds": meta.get("time_seconds"),
                "odds":        p.get(mid, {}),
            }
    return JSONResponse(live_matches)


@app.get("/history")
async def get_history(
    match_id:  str = Query(...,   description="Match ID (e.g. l3346070 or e3345964)"),
    selection: str = Query(...,   description="Selection name (player name)"),
    market:    str = Query("1X2", description="Market type"),
    limit:     int = Query(500,   ge=1, le=5000),
):
    """Return recent price history for one selection from SQLite."""
    return JSONResponse(query_history(match_id, selection, market, limit))
