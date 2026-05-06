"""
unibet_backend.py — FastAPI backend for the Unibet Tennis Odds feed
====================================================================
Port: 5003

Endpoints:
  GET /stream        Server-Sent Events — all price updates in real-time
  GET /prices        Current in-memory snapshot {match_id: {market: {sel: odd}}}
  GET /markets       Active matches + meta {match_id: {match, competition, ...}}
  GET /status        Feed stats
  GET /history       SQLite price history for one match+selection
  GET /live-scores   Current live match scores

Architecture:
  unibet_feed.run() → asyncio.Queue → consume_feed()
                                           ├─ updates in-memory prices dict
                                           ├─ fans out to SSE subscribers
                                           ├─ pushes rows to SQLite writer
                                           └─ tracks price movements (up/down/flash)
"""

import asyncio
import json
import logging
import sqlite3
import sys
import threading
import time
import queue as _queue
from contextlib import asynccontextmanager
from typing import AsyncGenerator

from fastapi import FastAPI, Query
from fastapi.responses import StreamingResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
import uvicorn

sys.path.insert(0, __import__("os").path.dirname(__import__("os").path.abspath(__file__)))
import unibet_feed

# ── CONFIG ──────────────────────────────────────────────────────────────────────
PORT    = 5003
DB_PATH = "unibet_tennis_prices.db"

# ── IN-MEMORY STATE ─────────────────────────────────────────────────────────────
prices: dict[str, dict[str, dict[str, float]]] = {}
prices_lock = asyncio.Lock()

_subscribers: set[asyncio.Queue] = set()

# Track price movements for visual indicators
_price_previous: dict[str, dict[str, dict[str, float]]] = {}
_price_movements: dict[str, dict[str, dict[str, str]]] = {}  # "up" | "down" | "steady"

# ── SQLITE WRITER (background daemon thread) ────────────────────────────────────
_db_queue: _queue.Queue = _queue.Queue()

_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS unibet_prices (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          REAL    NOT NULL,
    match_id    TEXT    NOT NULL,
    market      TEXT    NOT NULL,
    selection   TEXT    NOT NULL,
    odd         REAL    NOT NULL,
    match_name  TEXT,
    player_a    TEXT,
    player_b    TEXT,
    competition TEXT,
    match_date  TEXT,
    is_live     INTEGER DEFAULT 0
);
"""
_CREATE_INDEX = """
CREATE INDEX IF NOT EXISTS idx_unibet_match_ts ON unibet_prices(match_id, ts);
"""
_CREATE_META_TABLE = """
CREATE TABLE IF NOT EXISTS unibet_matches (
    match_id    TEXT PRIMARY KEY,
    match_name  TEXT,
    player_a    TEXT,
    player_b    TEXT,
    competition TEXT,
    match_date  TEXT,
    is_live     INTEGER DEFAULT 0,
    score_json TEXT,
    last_seen   REAL
);
"""

_INSERT_SQL = """
INSERT INTO unibet_prices
    (ts, match_id, market, selection, odd, match_name, player_a, player_b, competition, match_date, is_live)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""

_UPSERT_MATCH_SQL = """
INSERT OR REPLACE INTO unibet_matches
    (match_id, match_name, player_a, player_b, competition, match_date, is_live, score_json, last_seen)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
"""


def _sqlite_writer():
    """Daemon thread — consumes rows from _db_queue and batch-inserts."""
    con = sqlite3.connect(DB_PATH, check_same_thread=False)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    con.execute(_CREATE_TABLE)
    con.execute(_CREATE_INDEX)
    con.execute(_CREATE_META_TABLE)
    con.commit()

    batch: list[tuple] = []
    last_flush = time.time()

    while True:
        try:
            item = _db_queue.get(timeout=1.0)
            if item is None:
                break
            batch.append(item)
        except _queue.Empty:
            pass

        if batch and (len(batch) >= 100 or time.time() - last_flush >= 2.0):
            try:
                con.executemany(_INSERT_SQL, batch)
                con.commit()
                batch.clear()
                last_flush = time.time()
            except Exception as exc:
                logging.error(f"db write error: {exc}")

    if batch:
        try:
            con.executemany(_INSERT_SQL, batch)
            con.commit()
        except Exception:
            pass
    con.close()


# ── FEED CONSUMER ───────────────────────────────────────────────────────────────

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

    # Persist for next comparison
    _price_previous.setdefault(match_id, {}).setdefault(market, {}).update(odds)
    _price_movements.setdefault(match_id, {}).setdefault(market, {}).update(movements)
    return movements


async def consume_feed(q: asyncio.Queue):
    """Reads updates from feed, fans out to SSE clients, writes to SQLite."""
    while True:
        update: dict = await q.get()

        match_id = update["match_id"]
        market   = update["market"]
        odds     = update["odds"]
        meta     = update.get("meta", {})
        ts       = update.get("ts", time.time())

        # ── 1. Update in-memory prices ─────────────────────────────────────
        async with prices_lock:
            mdata = prices.setdefault(match_id, {})
            mdata.setdefault(market, {}).update(odds)

        # ── 2. Detect price movement direction ──────────────────────────────
        movements = _detect_movement(match_id, market, odds)

        # ── 3. Write each price to SQLite ──────────────────────────────────
        match_name  = meta.get("match", "")
        player_a    = meta.get("player_a", "")
        player_b    = meta.get("player_b", "")
        competition = meta.get("competition", "")
        match_date  = meta.get("date", "")
        is_live     = int(meta.get("live", False))

        for selection, odd in odds.items():
            _db_queue.put_nowait((
                ts, match_id, market, selection, odd,
                match_name, player_a, player_b, competition, match_date, is_live,
            ))

        # ── 5. Build SSE payload ───────────────────────────────────────────
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

        # ── 6. Fan-out to SSE subscribers ──────────────────────────────────
        dead: list[asyncio.Queue] = []
        for sub in list(_subscribers):
            try:
                sub.put_nowait(event_json)
            except asyncio.QueueFull:
                dead.append(sub)
        for sub in dead:
            _subscribers.discard(sub)


# ── LIFESPAN ────────────────────────────────────────────────────────────────────
_feed_queue: asyncio.Queue | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _feed_queue

    # Start SQLite writer daemon
    db_thread = threading.Thread(target=_sqlite_writer, daemon=True, name="unibet-db-writer")
    db_thread.start()
    logging.info("SQLite writer started")

    # Create feed queue and launch tasks
    _feed_queue = asyncio.Queue(maxsize=20_000)
    feed_task     = asyncio.create_task(unibet_feed.run(_feed_queue), name="unibet-feed")
    consumer_task = asyncio.create_task(consume_feed(_feed_queue), name="unibet-consumer")
    logging.info(f"Feed and consumer running on port {PORT}")

    yield

    # Graceful shutdown
    unibet_feed.stop()
    feed_task.cancel()
    consumer_task.cancel()
    _db_queue.put(None)
    db_thread.join(timeout=10)
    logging.info("Shutdown complete")


# ── APP ─────────────────────────────────────────────────────────────────────────
app = FastAPI(title="Unibet Tennis Feed", version="1.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── SSE /stream ─────────────────────────────────────────────────────────────────

@app.get("/stream")
async def stream_sse():
    """
    Server-Sent Events endpoint.
    Sends initial price snapshot then pushes live updates.
    Keepalive comment every 25s to prevent proxy timeouts.
    """
    q: asyncio.Queue[str] = asyncio.Queue(maxsize=500)
    _subscribers.add(q)

    async with prices_lock:
        snapshot = dict(prices)

    snapshot_json = json.dumps({
        "type":   "snapshot",
        "prices": snapshot,
        "meta":   dict(unibet_feed._meta),
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
            _subscribers.discard(q)

    return StreamingResponse(
        generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control":        "no-cache",
            "X-Accel-Buffering":    "no",
            "Connection":           "keep-alive",
        },
    )


# ── REST ENDPOINTS ──────────────────────────────────────────────────────────────

@app.get("/prices")
async def get_prices():
    """Full in-memory odds snapshot with movements."""
    async with prices_lock:
        p = dict(prices)
    return JSONResponse({
        "prices":    p,
        "movements": _price_movements,
        "ts":        time.time(),
    })


@app.get("/markets")
async def get_markets():
    """Active matches with full metadata."""
    all_meta = unibet_feed.get_all_meta()
    async with prices_lock:
        active_ids = set(prices.keys())
    result = {}
    for mid in active_ids:
        meta = all_meta.get(mid, {})
        odds = prices.get(mid, {})
        result[mid] = {
            **meta,
            "odds": odds,
            "movements": _price_movements.get(mid, {}),
        }
    return JSONResponse(result)


@app.get("/status")
async def get_status():
    """Feed health: stream count, update count, SSE clients."""
    stats = unibet_feed.get_stats()
    stats["sse_clients"]     = len(_subscribers)
    stats["prices_in_memory"] = len(prices)
    stats["db_queue_size"]    = _db_queue.qsize()
    return JSONResponse(stats)


@app.get("/live-scores")
async def get_live_scores():
    """Current scores for live tennis matches only."""
    all_meta = unibet_feed.get_all_meta()
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
                "odds":        prices.get(mid, {}),
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
    def _query() -> list[dict]:
        con = sqlite3.connect(DB_PATH, check_same_thread=True)
        try:
            rows = con.execute(
                """
                SELECT ts, odd
                FROM   unibet_prices
                WHERE  match_id  = ?
                  AND  market    = ?
                  AND  selection = ?
                ORDER  BY ts DESC
                LIMIT  ?
                """,
                (match_id, market, selection, limit),
            ).fetchall()
            return [{"ts": r[0], "odd": r[1]} for r in reversed(rows)]
        finally:
            con.close()

    rows = await asyncio.get_running_loop().run_in_executor(None, _query)
    return JSONResponse(rows)


# ── ENTRYPOINT ──────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")
    uvicorn.run(
        "unibet_backend:app",
        host="0.0.0.0",
        port=PORT,
        reload=False,
        log_level="info",
    )
