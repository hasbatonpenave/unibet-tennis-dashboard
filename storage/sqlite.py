"""SQLite storage for Unibet tennis price history."""

import logging
import sqlite3
import time
import queue as _queue

logger = logging.getLogger("unibet_storage")

DB_PATH = "unibet_tennis_prices.db"

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

_db_queue: _queue.Queue = _queue.Queue()
_read_con: sqlite3.Connection | None = None
_flush_interval: float = 0.5


def get_db_queue() -> _queue.Queue:
    return _db_queue


def open_read_connection(db_path: str = DB_PATH) -> sqlite3.Connection:
    """Open a persistent read-only connection (WAL mode, thread-safe for reads)."""
    global _read_con
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, check_same_thread=False)
    con.execute("PRAGMA query_only=ON")
    _read_con = con
    logger.info("Persistent read connection opened")
    return con


def get_read_connection() -> sqlite3.Connection | None:
    return _read_con


def close_read_connection() -> None:
    global _read_con
    if _read_con is not None:
        _read_con.close()
        _read_con = None


def _sqlite_writer(flush_interval: float = 0.5, batch_size: int = 100):
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

        if batch and (len(batch) >= batch_size or time.time() - last_flush >= flush_interval):
            try:
                con.executemany(_INSERT_SQL, batch)
                con.commit()
                batch.clear()
                last_flush = time.time()
            except Exception as exc:
                logger.error(f"db write error: {exc}")

    if batch:
        try:
            con.executemany(_INSERT_SQL, batch)
            con.commit()
        except Exception:
            pass
    con.close()


def query_history(
    match_id: str,
    selection: str,
    market: str = "1X2",
    limit: int = 500,
) -> list[dict]:
    """Return recent price history using the persistent read connection."""
    con = _read_con
    if con is None:
        con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, check_same_thread=False)
        try:
            rows = con.execute(
                """SELECT ts, odd FROM unibet_prices
                   WHERE match_id=? AND market=? AND selection=?
                   ORDER BY ts DESC LIMIT ?""",
                (match_id, market, selection, limit),
            ).fetchall()
            return [{"ts": r[0], "odd": r[1]} for r in reversed(rows)]
        finally:
            con.close()

    rows = con.execute(
        """SELECT ts, odd FROM unibet_prices
           WHERE match_id=? AND market=? AND selection=?
           ORDER BY ts DESC LIMIT ?""",
        (match_id, market, selection, limit),
    ).fetchall()
    return [{"ts": r[0], "odd": r[1]} for r in reversed(rows)]
