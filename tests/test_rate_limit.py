"""Unit tests for sliding-window rate limit policy."""

import sqlite3
import pytest

from mastery_service.db import SCHEMA_SQL
from mastery_service.rate_limit import (
    MAX_ATTEMPTS_PER_WINDOW,
    WINDOW_SECONDS,
    compute_retry_after,
    count_attempts_in_window,
    is_rate_limited,
)


@pytest.fixture
def db_conn():
    """In-memory SQLite connection initialized with the schema from db.py."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA_SQL)
    yield conn
    conn.close()


def insert_attempt(conn: sqlite3.Connection, student_id: str, created_at: float, skill_id: str = "fraction-addition", is_correct: int = 1):
    conn.execute(
        """
        INSERT INTO attempts (student_id, skill_id, is_correct, created_at)
        VALUES (?, ?, ?, ?)
        """,
        (student_id, skill_id, is_correct, created_at),
    )


def test_constants():
    assert MAX_ATTEMPTS_PER_WINDOW == 30
    assert WINDOW_SECONDS == 86400


def test_basic_boundary_29_vs_30(db_conn):
    now = 100000.0
    student_id = "student-test-basic"

    for i in range(29):
        insert_attempt(db_conn, student_id, created_at=now - (i * 10))

    assert count_attempts_in_window(db_conn, student_id, now) == 29
    assert is_rate_limited(db_conn, student_id, now) is False

    insert_attempt(db_conn, student_id, created_at=now - 300)

    assert count_attempts_in_window(db_conn, student_id, now) == 30
    assert is_rate_limited(db_conn, student_id, now) is True


def test_lower_window_boundary(db_conn):
    now = 100000.0
    student_id = "student-lower-bound"

    # Exactly at now - WINDOW_SECONDS: expired and must NOT be counted
    insert_attempt(db_conn, student_id, created_at=now - WINDOW_SECONDS)
    assert count_attempts_in_window(db_conn, student_id, now) == 0

    # Exactly at now - WINDOW_SECONDS + 1: inside and MUST be counted
    insert_attempt(db_conn, student_id, created_at=now - WINDOW_SECONDS + 1)
    assert count_attempts_in_window(db_conn, student_id, now) == 1


def test_upper_window_boundary(db_conn):
    now = 100000.0
    student_id = "student-upper-bound"

    # Exactly at now: must be counted
    insert_attempt(db_conn, student_id, created_at=now)
    assert count_attempts_in_window(db_conn, student_id, now) == 1

    # Future attempt after now: must NOT be counted at now
    insert_attempt(db_conn, student_id, created_at=now + 1.0)
    assert count_attempts_in_window(db_conn, student_id, now) == 1


def test_cross_student_isolation(db_conn):
    now = 100000.0
    student_a = "student-ananya"
    student_b = "student-rohan"

    for i in range(30):
        insert_attempt(db_conn, student_a, created_at=now - (i * 50))

    for i in range(5):
        insert_attempt(db_conn, student_b, created_at=now - (i * 50))

    assert count_attempts_in_window(db_conn, student_a, now) == 30
    assert is_rate_limited(db_conn, student_a, now) is True

    assert count_attempts_in_window(db_conn, student_b, now) == 5
    assert is_rate_limited(db_conn, student_b, now) is False


def test_retry_after(db_conn):
    now = 100000.0
    student_id = "student-retry"

    oldest_created = now - WINDOW_SECONDS + 100
    insert_attempt(db_conn, student_id, created_at=oldest_created)
    for i in range(1, 30):
        insert_attempt(db_conn, student_id, created_at=oldest_created + i)

    retry_after = compute_retry_after(db_conn, student_id, now)
    assert retry_after == 100

    # Fractional timestamp test: 100.2 seconds remaining should ceiling to 101
    student_frac = "student-fractional"
    oldest_frac = now - WINDOW_SECONDS + 100.2
    insert_attempt(db_conn, student_frac, created_at=oldest_frac)
    assert compute_retry_after(db_conn, student_frac, now) == 101


def test_no_attempts_returns_zero(db_conn):
    now = 100000.0
    student_id = "student-no-attempts"

    assert compute_retry_after(db_conn, student_id, now) == 0
    assert count_attempts_in_window(db_conn, student_id, now) == 0
    assert is_rate_limited(db_conn, student_id, now) is False


def test_determinism(db_conn):
    now = 100000.0
    student_id = "student-det"

    for i in range(15):
        insert_attempt(db_conn, student_id, created_at=now - (i * 100))

    count_1 = count_attempts_in_window(db_conn, student_id, now)
    count_2 = count_attempts_in_window(db_conn, student_id, now)
    assert count_1 == count_2

    retry_1 = compute_retry_after(db_conn, student_id, now)
    retry_2 = compute_retry_after(db_conn, student_id, now)
    assert retry_1 == retry_2

    limited_1 = is_rate_limited(db_conn, student_id, now)
    limited_2 = is_rate_limited(db_conn, student_id, now)
    assert limited_1 == limited_2


def test_no_speculative_writes(db_conn):
    now = 100000.0
    student_id = "student-read-only"

    for i in range(30):
        insert_attempt(db_conn, student_id, created_at=now - (i * 10))

    before = db_conn.execute("SELECT * FROM attempts ORDER BY id").fetchall()
    before_rows = [tuple(row) for row in before]

    _ = count_attempts_in_window(db_conn, student_id, now)
    _ = is_rate_limited(db_conn, student_id, now)
    _ = compute_retry_after(db_conn, student_id, now)

    after = db_conn.execute("SELECT * FROM attempts ORDER BY id").fetchall()
    after_rows = [tuple(row) for row in after]

    assert before_rows == after_rows
