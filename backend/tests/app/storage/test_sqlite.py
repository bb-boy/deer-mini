import sqlite3

import pytest

from app.infrastructure import database


def test_execution_error_is_classified_and_transaction_rolls_back(tmp_path, monkeypatch):
    from app.storage.errors import StorageError
    monkeypatch.setattr(database, "DATABASE_PATH", tmp_path / "db")
    with database.connect() as conn:
        conn.execute("CREATE TABLE sample (value TEXT UNIQUE)")
    with pytest.raises(StorageError) as caught:
        with database.connect() as conn:
            conn.execute("INSERT INTO sample VALUES ('private')")
            conn.execute("INSERT INTO absent VALUES ('private')")
    assert caught.value.stage == "execute" and caught.value.commit_state == "not_committed"
    assert "private" not in str(caught.value)
    with database.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM sample").fetchone()[0] == 0


def test_integrity_errors_keep_business_catch_compatibility(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DATABASE_PATH", tmp_path / "db")
    with database.connect() as conn:
        conn.execute("CREATE TABLE sample (value TEXT UNIQUE)")
        conn.execute("INSERT INTO sample VALUES ('duplicate')")
    with pytest.raises(sqlite3.IntegrityError):
        with database.connect() as conn:
            conn.cursor().execute("INSERT INTO sample VALUES ('duplicate')")


def test_commit_busy_is_uncertain_and_retry_is_left_to_caller(tmp_path, monkeypatch):
    from app.storage.errors import StorageError
    monkeypatch.setattr(database, "DATABASE_PATH", tmp_path / "db")
    with database.connect() as conn:
        conn.execute("CREATE TABLE sample (value TEXT)")
        conn.execute("INSERT INTO sample VALUES ('old')")
    reader = sqlite3.connect(tmp_path / "db")
    reader.execute("BEGIN")
    reader.execute("SELECT * FROM sample").fetchone()
    writer = database.connect()
    writer.execute("PRAGMA busy_timeout = 0")
    try:
        with pytest.raises(StorageError) as caught:
            with writer:
                writer.execute("INSERT INTO sample VALUES ('new')")
        assert caught.value.category == "busy"
        assert caught.value.stage == "commit" and caught.value.commit_state == "uncertain"
        assert caught.value.recovery == "verify"
    finally:
        reader.close()
        writer.close()


def test_script_error_reports_possible_earlier_commits(tmp_path, monkeypatch):
    from app.storage.errors import StorageError
    monkeypatch.setattr(database, "DATABASE_PATH", tmp_path / "db")
    with database.connect() as conn:
        with pytest.raises(StorageError) as caught:
            conn.executescript("CREATE TABLE sample (value TEXT); INSERT INTO absent VALUES ('private');")
        assert caught.value.stage == "execute_script" and caught.value.commit_state == "uncertain"
        assert conn.execute("SELECT COUNT(*) FROM sample").fetchone()[0] == 0


def test_closed_connection_execution_is_classified(tmp_path, monkeypatch):
    from app.storage.errors import StorageError
    monkeypatch.setattr(database, "DATABASE_PATH", tmp_path / "db")
    conn = database.connect()
    conn.close()
    with pytest.raises(StorageError):
        conn.execute("SELECT 1")
