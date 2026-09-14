"""Domain service for atomic attempt, mastery, and milestone processing."""

from dataclasses import dataclass
import sqlite3
import time

from mastery_service.rate_limit import (
    MAX_ATTEMPTS_PER_WINDOW,
    compute_retry_after,
    count_attempts_in_window,
)
from mastery_service.scoring import BASELINE_SEED, update_mastery


class RateLimitExceeded(Exception):
    """Raised when a student exceeds the allowed attempts in the rolling window."""

    def __init__(self, retry_after_seconds: int, detail: str = "Rate limit exceeded") -> None:
        self.retry_after_seconds = retry_after_seconds
        self.detail = detail
        super().__init__(detail)


class IdempotencyConflict(Exception):
    """Raised when an idempotency key is already used with different attempt parameters."""

    def __init__(
        self,
        detail: str = "Idempotency key already used with different attempt parameters",
    ) -> None:
        self.detail = detail
        super().__init__(detail)


@dataclass(frozen=True)
class AttemptResult:
    skill_id: str
    mastery: float
    milestone_reached: bool
    is_replayed: bool = False


def record_attempt(
    conn: sqlite3.Connection,
    student_id: str,
    skill_id: str,
    is_correct: bool,
    now: float | None = None,
    idempotency_key: str | None = None,
) -> AttemptResult:
    
    if now is None:
        now = time.time()

    # BEGIN IMMEDIATE holds the SQLite write reservation across the check-and-write sequence,
    # preventing another writer from bypassing the rate-limit check or concurrent idempotency check.
    conn.execute("BEGIN IMMEDIATE")
    try:
        if idempotency_key is not None:
            cur_key = conn.execute(
                """
                SELECT skill_id, is_correct, mastery, milestone_reached
                FROM idempotency_keys
                WHERE student_id = ? AND idempotency_key = ?
                """,
                (student_id, idempotency_key),
            )
            existing = cur_key.fetchone()
            if existing is not None:
                stored_skill_id = existing["skill_id"]
                stored_is_correct = bool(existing["is_correct"])
                if stored_skill_id == skill_id and stored_is_correct == is_correct:
                    conn.execute("COMMIT")
                    return AttemptResult(
                        skill_id=stored_skill_id,
                        mastery=float(existing["mastery"]),
                        milestone_reached=bool(existing["milestone_reached"]),
                        is_replayed=True,
                    )
                else:
                    conn.execute("COMMIT")
                    raise IdempotencyConflict(
                        detail="Idempotency key already used with different attempt parameters"
                    )

        attempts_in_window = count_attempts_in_window(conn, student_id, now)
        if attempts_in_window >= MAX_ATTEMPTS_PER_WINDOW:
            retry_after = compute_retry_after(conn, student_id, now)
            raise RateLimitExceeded(retry_after_seconds=retry_after)

        cur = conn.execute(
            """
            SELECT score, last_practiced_at
            FROM mastery
            WHERE student_id = ? AND skill_id = ?
            """,
            (student_id, skill_id),
        )
        row = cur.fetchone()

        if row is not None:
            previous_score: float | None = float(row["score"])
            last_practiced_at: float | None = float(row["last_practiced_at"])
            score_for_milestone = previous_score
        else:
            previous_score = None
            last_practiced_at = None
            score_for_milestone = BASELINE_SEED

        new_score = update_mastery(
            previous_score=previous_score,
            last_practiced_at=last_practiced_at,
            is_correct=is_correct,
            now=now,
        )

        conn.execute(
            """
            INSERT INTO attempts (student_id, skill_id, is_correct, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (student_id, skill_id, 1 if is_correct else 0, now),
        )

        conn.execute(
            """
            INSERT INTO mastery (student_id, skill_id, score, last_practiced_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT (student_id, skill_id) DO UPDATE SET
                score = excluded.score,
                last_practiced_at = excluded.last_practiced_at
            """,
            (student_id, skill_id, new_score, now),
        )

        cur_notif = conn.execute(
            """
            SELECT 1 FROM notifications
            WHERE student_id = ? AND skill_id = ?
            """,
            (student_id, skill_id),
        )
        already_notified = cur_notif.fetchone() is not None

        milestone_reached = False
        if score_for_milestone <= 80.0 and new_score > 80.0:
            if not already_notified:
                conn.execute(
                    """
                    INSERT INTO notifications (student_id, skill_id, reached_at)
                    VALUES (?, ?, ?)
                    """,
                    (student_id, skill_id, now),
                )
                milestone_reached = True

        if idempotency_key is not None:
            conn.execute(
                """
                INSERT INTO idempotency_keys (
                    student_id, idempotency_key, skill_id, is_correct,
                    mastery, milestone_reached, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    student_id,
                    idempotency_key,
                    skill_id,
                    1 if is_correct else 0,
                    new_score,
                    1 if milestone_reached else 0,
                    now,
                ),
            )

        conn.execute("COMMIT")
        return AttemptResult(
            skill_id=skill_id,
            mastery=new_score,
            milestone_reached=milestone_reached,
        )
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
