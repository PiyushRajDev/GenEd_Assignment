"""Sliding-window rate limiting policy for student attempts."""

import math
import sqlite3

MAX_ATTEMPTS_PER_WINDOW = 30
WINDOW_SECONDS = 86400


def count_attempts_in_window(
    conn: sqlite3.Connection,
    student_id: str,
    now: float,
) -> int:

    cursor = conn.execute(
        """
        SELECT COUNT(*)
        FROM attempts
        WHERE student_id = ?
          AND created_at > ?
          AND created_at <= ?
        """,
        (student_id, now - WINDOW_SECONDS, now),
    )
    row = cursor.fetchone()
    return int(row[0]) if row else 0


def compute_retry_after(
    conn: sqlite3.Connection,
    student_id: str,
    now: float,
) -> int:
    cursor = conn.execute(
        """
        SELECT MIN(created_at)
        FROM attempts
        WHERE student_id = ?
          AND created_at > ?
          AND created_at <= ?
        """,
        (student_id, now - WINDOW_SECONDS, now),
    )
    row = cursor.fetchone()
    if row is None or row[0] is None:
        return 0

    oldest_created_at = float(row[0])
    retry_after = math.ceil((oldest_created_at + WINDOW_SECONDS) - now)
    return max(0, int(retry_after))


def is_rate_limited(
    conn: sqlite3.Connection,
    student_id: str,
    now: float,
) -> bool:

    return count_attempts_in_window(conn, student_id, now) >= MAX_ATTEMPTS_PER_WINDOW
