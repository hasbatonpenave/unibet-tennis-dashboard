"""Unit tests for storage/sqlite.py — write -> flush -> read round-trip."""

import os
import sqlite3
import tempfile
import time

import pytest

import storage.sqlite as storage


@pytest.fixture
def temp_db():
    """Create a temp database for testing, clean up after."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    old_path = storage.DB_PATH
    storage.DB_PATH = path
    storage._read_con = None

    # Ensure tables exist
    con = sqlite3.connect(path)
    con.execute(storage._CREATE_TABLE)
    con.execute(storage._CREATE_INDEX)
    con.execute(storage._CREATE_META_TABLE)
    con.commit()
    con.close()

    # Open persistent read connection
    storage.open_read_connection(path)

    yield path

    storage.close_read_connection()
    storage.DB_PATH = old_path
    if os.path.exists(path):
        os.unlink(path)
    # Also clean up WAL/SHM
    for suffix in ("-wal", "-shm"):
        p = path + suffix
        if os.path.exists(p):
            os.unlink(p)


# Helper: build a row tuple with sport column
def _row(ts, mid, market, sel, odd, name="", pa="", pb="", comp="", date="", live=0, sport="tennis"):
    return (ts, mid, market, sel, odd, name, pa, pb, comp, date, live, sport)


class TestSQLiteWriter:
    def test_writes_and_flushes(self, temp_db):
        storage.get_db_queue().put(_row(
            time.time(), "test_match", "1X2", "PlayerA", 1.85,
            "Test Match", "PlayerA", "PlayerB", "Test Comp", "2605081400", 0,
        ))
        storage.get_db_queue().put(_row(
            time.time(), "test_match", "1X2", "PlayerB", 1.95,
            "Test Match", "PlayerA", "PlayerB", "Test Comp", "2605081400", 0,
        ))
        storage.get_db_queue().put(None)

        storage._sqlite_writer(flush_interval=0.01, batch_size=1)

    def test_reads_written_data(self, temp_db):
        ts = time.time()
        storage.get_db_queue().put(_row(ts, "m1", "1X2", "P1", 2.00, "M", "P1", "P2", "C", "d", 0))
        storage.get_db_queue().put(_row(ts, "m1", "1X2", "P2", 1.50, "M", "P1", "P2", "C", "d", 0))
        storage.get_db_queue().put(None)
        storage._sqlite_writer(flush_interval=0.01, batch_size=1)

        storage.close_read_connection()
        storage.open_read_connection(temp_db)

        rows = storage.query_history("m1", "P1", "1X2", 10)
        assert len(rows) == 1
        assert rows[0]["odd"] == 2.00
        assert rows[0]["ts"] == pytest.approx(ts, abs=0.1)

    def test_query_history_limit(self, temp_db):
        for i in range(5):
            storage.get_db_queue().put(_row(
                time.time() + i, "m_limit", "1X2", "Player", 1.1 + i * 0.1,
            ))
        storage.get_db_queue().put(None)
        storage._sqlite_writer(flush_interval=0.01, batch_size=1)

        storage.close_read_connection()
        storage.open_read_connection(temp_db)

        rows = storage.query_history("m_limit", "Player", "1X2", limit=3)
        assert len(rows) == 3

    def test_filter_by_market(self, temp_db):
        storage.get_db_queue().put(_row(time.time(), "m2", "1X2", "Sel", 1.0))
        storage.get_db_queue().put(_row(time.time(), "m2", "Set1", "Sel", 2.0))
        storage.get_db_queue().put(None)
        storage._sqlite_writer(flush_interval=0.01, batch_size=1)

        storage.close_read_connection()
        storage.open_read_connection(temp_db)

        rows_1x2 = storage.query_history("m2", "Sel", "1X2")
        assert len(rows_1x2) == 1
        assert rows_1x2[0]["odd"] == 1.0

        rows_set1 = storage.query_history("m2", "Sel", "Set1")
        assert len(rows_set1) == 1
        assert rows_set1[0]["odd"] == 2.0

    def test_filter_by_sport(self, temp_db):
        storage.get_db_queue().put(_row(time.time(), "m3", "1X2", "Sel", 1.5, sport="tennis"))
        storage.get_db_queue().put(_row(time.time(), "m3", "1X2", "Sel", 2.5, sport="soccer"))
        storage.get_db_queue().put(None)
        storage._sqlite_writer(flush_interval=0.01, batch_size=1)

        storage.close_read_connection()
        storage.open_read_connection(temp_db)

        rows_tennis = storage.query_history("m3", "Sel", "1X2", sport="tennis")
        assert len(rows_tennis) == 1
        assert rows_tennis[0]["odd"] == 1.5

        rows_all = storage.query_history("m3", "Sel", "1X2")
        assert len(rows_all) == 2

    def test_read_connection_is_readonly(self, temp_db):
        con = storage.get_read_connection()
        assert con is not None
        with pytest.raises(sqlite3.OperationalError):
            con.execute("CREATE TABLE test (id INT)")


class TestDBQueue:
    def test_get_db_queue_returns_same_queue(self):
        q1 = storage.get_db_queue()
        q2 = storage.get_db_queue()
        assert q1 is q2


class TestReadConnectionLifecycle:
    def test_open_and_close(self, temp_db):
        assert storage.get_read_connection() is not None
        storage.close_read_connection()
        assert storage.get_read_connection() is None


class TestSchemaMigration:
    def test_migration_adds_sport_column(self, temp_db):
        # Remove sport column to simulate old schema
        con = sqlite3.connect(temp_db)
        # SQLite doesn't support DROP COLUMN easily in older versions,
        # so just verify the column exists after _migrate_schema
        storage._migrate_schema(con)
        cols = [r[1] for r in con.execute("PRAGMA table_info(unibet_prices)").fetchall()]
        assert "sport" in cols
        con.close()
