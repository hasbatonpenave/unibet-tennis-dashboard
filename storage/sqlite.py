"""SQLite storage for Unibet sports price history."""

import logging
import os
import sqlite3
import time
import queue as _queue

from config import settings

logger = logging.getLogger("unibet_storage")

DB_PATH = settings.db_path

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
    is_live     INTEGER DEFAULT 0,
    sport       TEXT    DEFAULT 'tennis'
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
    sport       TEXT DEFAULT 'tennis',
    score_json  TEXT,
    last_seen   REAL
);
"""

_INSERT_SQL = """
INSERT INTO unibet_prices
    (ts, match_id, market, selection, odd, match_name, player_a, player_b, competition, match_date, is_live, sport)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""

_UPSERT_MATCH_SQL = """
INSERT OR REPLACE INTO unibet_matches
    (match_id, match_name, player_a, player_b, competition, match_date, is_live, sport, score_json, last_seen)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""

_db_queue: _queue.Queue = _queue.Queue()
_read_con: sqlite3.Connection | None = None
_flush_interval: float = 0.5


def get_db_queue() -> _queue.Queue:
    return _db_queue


def _migrate_schema(con: sqlite3.Connection) -> None:
    """Add sport column if missing (migration from tennis-only schema)."""
    cols = [r[1] for r in con.execute("PRAGMA table_info(unibet_prices)").fetchall()]
    if "sport" not in cols:
        con.execute("ALTER TABLE unibet_prices ADD COLUMN sport TEXT DEFAULT 'tennis'")
        logger.info("Added sport column to unibet_prices")
    meta_cols = [r[1] for r in con.execute("PRAGMA table_info(unibet_matches)").fetchall()]
    if "sport" not in meta_cols:
        con.execute("ALTER TABLE unibet_matches ADD COLUMN sport TEXT DEFAULT 'tennis'")
        logger.info("Added sport column to unibet_matches")


def open_read_connection(db_path: str = DB_PATH) -> sqlite3.Connection:
    """Open a persistent read-only connection (WAL mode, thread-safe for reads)."""
    global _read_con
    # Ensure the DB file exists before opening read-only
    if not os.path.exists(db_path):
        _ensure_db_exists(db_path)
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, check_same_thread=False)
    con.execute("PRAGMA query_only=ON")
    _read_con = con
    logger.info("Persistent read connection opened")
    return con


def _ensure_db_exists(db_path: str) -> None:
    """Create the database and tables if the file doesn't exist yet."""
    con = sqlite3.connect(db_path)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    con.execute(_CREATE_TABLE)
    con.execute(_CREATE_INDEX)
    con.execute(_CREATE_META_TABLE)
    _migrate_schema(con)
    con.commit()
    con.close()
    logger.info(f"Created database at {db_path}")


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
    _migrate_schema(con)
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
    sport: str | None = None,
) -> list[dict]:
    """Return recent price history using the persistent read connection."""
    con = _read_con
    if con is None:
        con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, check_same_thread=False)
        try:
            if sport:
                rows = con.execute(
                    """SELECT ts, odd FROM unibet_prices
                       WHERE match_id=? AND market=? AND selection=? AND sport=?
                       ORDER BY ts DESC LIMIT ?""",
                    (match_id, market, selection, sport, limit),
                ).fetchall()
            else:
                rows = con.execute(
                    """SELECT ts, odd FROM unibet_prices
                       WHERE match_id=? AND market=? AND selection=?
                       ORDER BY ts DESC LIMIT ?""",
                    (match_id, market, selection, limit),
                ).fetchall()
            return [{"ts": r[0], "odd": r[1]} for r in reversed(rows)]
        finally:
            con.close()

    if sport:
        rows = con.execute(
            """SELECT ts, odd FROM unibet_prices
               WHERE match_id=? AND market=? AND selection=? AND sport=?
               ORDER BY ts DESC LIMIT ?""",
            (match_id, market, selection, sport, limit),
        ).fetchall()
    else:
        rows = con.execute(
            """SELECT ts, odd FROM unibet_prices
               WHERE match_id=? AND market=? AND selection=?
               ORDER BY ts DESC LIMIT ?""",
            (match_id, market, selection, limit),
        ).fetchall()
    return [{"ts": r[0], "odd": r[1]} for r in reversed(rows)]
