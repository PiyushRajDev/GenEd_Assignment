"""Database connection and initialization module for SQLite."""

import os
import sqlite3
from typing import Generator

def _resolve_db_path(db_path: str | None = None) -> str:
    return db_path or os.environ.get("MASTERY_DB_PATH", "mastery.db")


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS attempts (
    id          INTEGER PRIMARY KEY,
    student_id  TEXT NOT NULL,
    skill_id    TEXT NOT NULL,
    is_correct  INTEGER NOT NULL CHECK (is_correct IN (0, 1)),
    created_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_attempts_student_created
    ON attempts(student_id, created_at);

CREATE TABLE IF NOT EXISTS mastery (
    student_id        TEXT NOT NULL,
    skill_id          TEXT NOT NULL,
    score             REAL NOT NULL,
    last_practiced_at REAL NOT NULL,
    PRIMARY KEY (student_id, skill_id)
);

CREATE TABLE IF NOT EXISTS notifications (
    id          INTEGER PRIMARY KEY,
    student_id  TEXT NOT NULL,
    skill_id    TEXT NOT NULL,
    reached_at  REAL NOT NULL,
    UNIQUE (student_id, skill_id)
);
"""


def get_connection(db_path: str | None = None) -> sqlite3.Connection:
   
    path = _resolve_db_path(db_path)
    conn = sqlite3.connect(
    path,
    isolation_level=None,
    check_same_thread=False,
)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("PRAGMA busy_timeout=5000;")
    return conn


def init_db(db_path: str | None = None) -> None:
    
    conn = get_connection(db_path)
    try:
        conn.executescript(SCHEMA_SQL)
    finally:
        conn.close()


def get_db() -> Generator[sqlite3.Connection, None, None]:
    
    conn = get_connection()
    try:
        yield conn
    finally:
        conn.close()
