"""Tests for atomic attempt recording, mastery updates, milestone notifications, and rate limiting."""

import os
import sqlite3
import time
import pytest
from fastapi.testclient import TestClient

from mastery_service.attempts import AttemptResult, RateLimitExceeded, record_attempt
from mastery_service.db import SCHEMA_SQL, get_connection, init_db
from mastery_service.main import app
from mastery_service.rate_limit import WINDOW_SECONDS
from mastery_service.seed_data import SKILL_IDS, TOKENS

SKILL_A = SKILL_IDS[0]  # "math.fractions.add-subtract"
SKILL_B = SKILL_IDS[1]  # "math.fractions.multiply-divide"


@pytest.fixture
def db_conn():
    """In-memory SQLite connection with schema initialized and autocommit mode."""
    conn = sqlite3.connect(":memory:", isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA_SQL)
    yield conn
    conn.close()


@pytest.fixture
def test_db_path(tmp_path, monkeypatch):
    """File-based SQLite database for API and concurrency tests."""
    db_file = str(tmp_path / "test_mastery.db")
    monkeypatch.setenv("MASTERY_DB_PATH", db_file)
    init_db(db_file)
    yield db_file
    if os.path.exists(db_file):
        try:
            os.remove(db_file)
        except OSError:
            pass


# -------------------------------------------------------------------------
# 1. First correct attempt
# -------------------------------------------------------------------------
def test_first_correct_attempt(db_conn):
    now = 1000.0
    res = record_attempt(db_conn, "student-ananya", SKILL_A, is_correct=True, now=now)

    assert isinstance(res, AttemptResult)
    assert res.skill_id == SKILL_A
    assert res.mastery == 60.0
    assert res.milestone_reached is False

    # Attempt persisted
    attempts = db_conn.execute("SELECT * FROM attempts WHERE student_id = ?", ("student-ananya",)).fetchall()
    assert len(attempts) == 1
    assert attempts[0]["skill_id"] == SKILL_A
    assert attempts[0]["is_correct"] == 1
    assert attempts[0]["created_at"] == now

    # Mastery persisted
    mastery = db_conn.execute("SELECT * FROM mastery WHERE student_id = ?", ("student-ananya",)).fetchall()
    assert len(mastery) == 1
    assert mastery[0]["score"] == 60.0
    assert mastery[0]["last_practiced_at"] == now

    # No milestone notification
    notifications = db_conn.execute("SELECT * FROM notifications WHERE student_id = ?", ("student-ananya",)).fetchall()
    assert len(notifications) == 0


# -------------------------------------------------------------------------
# 2. First incorrect attempt
# -------------------------------------------------------------------------
def test_first_incorrect_attempt(db_conn):
    now = 1000.0
    res = record_attempt(db_conn, "student-ananya", SKILL_A, is_correct=False, now=now)

    assert res.mastery == 40.0
    assert res.milestone_reached is False

    mastery = db_conn.execute("SELECT * FROM mastery WHERE student_id = ?", ("student-ananya",)).fetchall()
    assert len(mastery) == 1
    assert mastery[0]["score"] == 40.0
    assert mastery[0]["last_practiced_at"] == now


# -------------------------------------------------------------------------
# 3. Existing mastery update
# -------------------------------------------------------------------------
def test_existing_mastery_update(db_conn):
    # Seed existing score of 70.0 at t=1000.0
    db_conn.execute(
        "INSERT INTO mastery (student_id, skill_id, score, last_practiced_at) VALUES (?, ?, ?, ?)",
        ("student-ananya", SKILL_A, 70.0, 1000.0),
    )

    # Next attempt at t=1000.0 (zero decay elapsed): 70 + 0.2*(100 - 70) = 76.0
    res = record_attempt(db_conn, "student-ananya", SKILL_A, is_correct=True, now=1000.0)

    assert res.mastery == 76.0
    assert res.milestone_reached is False

    row = db_conn.execute(
        "SELECT score, last_practiced_at FROM mastery WHERE student_id = ? AND skill_id = ?",
        ("student-ananya", SKILL_A),
    ).fetchone()
    assert row["score"] == 76.0
    assert row["last_practiced_at"] == 1000.0


# -------------------------------------------------------------------------
# 4. Milestone crossing
# -------------------------------------------------------------------------
def test_milestone_crossing(db_conn):
    # Seed mastery at 78.0 (<= 80.0)
    db_conn.execute(
        "INSERT INTO mastery (student_id, skill_id, score, last_practiced_at) VALUES (?, ?, ?, ?)",
        ("student-ananya", SKILL_A, 78.0, 1000.0),
    )

    # Correct attempt at t=1000.0: 78.0 + 0.2*(100 - 78.0) = 82.4 (> 80.0)
    res = record_attempt(db_conn, "student-ananya", SKILL_A, is_correct=True, now=1000.0)

    assert res.mastery == 82.4
    assert res.milestone_reached is True

    notifs = db_conn.execute(
        "SELECT * FROM notifications WHERE student_id = ? AND skill_id = ?",
        ("student-ananya", SKILL_A),
    ).fetchall()
    assert len(notifs) == 1
    assert notifs[0]["reached_at"] == 1000.0


# -------------------------------------------------------------------------
# 5. No false milestone
# -------------------------------------------------------------------------
def test_no_false_milestone(db_conn):
    # Seed mastery at 82.0 (> 80.0)
    db_conn.execute(
        "INSERT INTO mastery (student_id, skill_id, score, last_practiced_at) VALUES (?, ?, ?, ?)",
        ("student-ananya", SKILL_A, 82.0, 1000.0),
    )

    # Correct attempt: 82.0 + 0.2*(100 - 82.0) = 85.6 (> 80.0). Both previous and new > 80.
    res = record_attempt(db_conn, "student-ananya", SKILL_A, is_correct=True, now=1000.0)

    assert res.mastery == 85.6
    assert res.milestone_reached is False

    notifs = db_conn.execute("SELECT * FROM notifications").fetchall()
    assert len(notifs) == 0


# -------------------------------------------------------------------------
# 6. Duplicate milestone avoidance
# -------------------------------------------------------------------------
def test_duplicate_milestone_avoidance(db_conn):
    # Student reaches milestone initially
    db_conn.execute(
        "INSERT INTO mastery (student_id, skill_id, score, last_practiced_at) VALUES (?, ?, ?, ?)",
        ("student-ananya", SKILL_A, 78.0, 1000.0),
    )
    res1 = record_attempt(db_conn, "student-ananya", SKILL_A, is_correct=True, now=1000.0)
    assert res1.milestone_reached is True

    # Notification exists
    notifs = db_conn.execute("SELECT * FROM notifications").fetchall()
    assert len(notifs) == 1

    # Later, score decays below 80 (e.g. manually set to 75.0)
    db_conn.execute(
        "UPDATE mastery SET score = 75.0, last_practiced_at = 1000.0 WHERE student_id = ? AND skill_id = ?",
        ("student-ananya", SKILL_A),
    )

    # Student practice brings score to 75 + 0.2*(100 - 75) = 80.0 (or with 78 -> 82.4)
    db_conn.execute(
        "UPDATE mastery SET score = 78.0, last_practiced_at = 2000.0 WHERE student_id = ? AND skill_id = ?",
        ("student-ananya", SKILL_A),
    )
    res2 = record_attempt(db_conn, "student-ananya", SKILL_A, is_correct=True, now=2000.0)
    assert res2.mastery == 82.4
    # Even though previous <= 80 and new > 80, notification already exists: milestone_reached must be False
    assert res2.milestone_reached is False

    # Still exactly 1 notification
    notifs_after = db_conn.execute("SELECT * FROM notifications").fetchall()
    assert len(notifs_after) == 1
    assert notifs_after[0]["reached_at"] == 1000.0


# -------------------------------------------------------------------------
# 7. Rate limit enforcement
# -------------------------------------------------------------------------
def test_rate_limit_enforcement(db_conn):
    now = 100000.0
    student_id = "student-rate-test"

    # Insert 29 attempts in rolling window
    for i in range(29):
        db_conn.execute(
            "INSERT INTO attempts (student_id, skill_id, is_correct, created_at) VALUES (?, ?, ?, ?)",
            (student_id, SKILL_A, 1, now - (i * 10)),
        )

    # 30th attempt succeeds
    res = record_attempt(db_conn, student_id, SKILL_A, is_correct=True, now=now)
    assert res.mastery == 60.0

    count = db_conn.execute("SELECT COUNT(*) FROM attempts WHERE student_id = ?", (student_id,)).fetchone()[0]
    assert count == 30

    # 31st attempt raises RateLimitExceeded
    with pytest.raises(RateLimitExceeded) as exc_info:
        record_attempt(db_conn, student_id, SKILL_A, is_correct=True, now=now + 1)

    assert exc_info.value.retry_after_seconds > 0

    # The rejected attempt was NOT persisted
    count_after = db_conn.execute("SELECT COUNT(*) FROM attempts WHERE student_id = ?", (student_id,)).fetchone()[0]
    assert count_after == 30


# -------------------------------------------------------------------------
# 8. Rolling-window boundaries
# -------------------------------------------------------------------------
def test_rolling_window_boundaries(db_conn):
    now = 100000.0
    student_id = "student-window-test"

    # Exactly 30 attempts at now - WINDOW_SECONDS (expired from window)
    for i in range(30):
        db_conn.execute(
            "INSERT INTO attempts (student_id, skill_id, is_correct, created_at) VALUES (?, ?, ?, ?)",
            (student_id, SKILL_A, 1, now - WINDOW_SECONDS),
        )

    # Submission at now must succeed because all 30 are outside (now - WINDOW_SECONDS, now]
    res = record_attempt(db_conn, student_id, SKILL_A, is_correct=True, now=now)
    assert res.mastery == 60.0

    # Attempt exactly at now is inside the window
    count = db_conn.execute(
        "SELECT COUNT(*) FROM attempts WHERE student_id = ? AND created_at > ? AND created_at <= ?",
        (student_id, now - WINDOW_SECONDS, now),
    ).fetchone()[0]
    assert count == 1


# -------------------------------------------------------------------------
# 9. Transaction rollback on failure
# -------------------------------------------------------------------------
def test_transaction_rollback_on_failure(db_conn):
    # Install an abort trigger on notifications table
    db_conn.execute(
        """
        CREATE TRIGGER abort_notification_insert
        BEFORE INSERT ON notifications
        BEGIN
            SELECT RAISE(ABORT, 'Simulated failure before notification commit');
        END;
        """
    )

    # Seed score <= 80 so milestone will attempt to insert notification
    db_conn.execute(
        "INSERT INTO mastery (student_id, skill_id, score, last_practiced_at) VALUES (?, ?, ?, ?)",
        ("student-ananya", SKILL_A, 78.0, 1000.0),
    )

    # Attempt to record milestone crossing — trigger causes SQLite to abort
    with pytest.raises(sqlite3.IntegrityError, match="Simulated failure"):
        record_attempt(db_conn, "student-ananya", SKILL_A, is_correct=True, now=1000.0)

    # Verify atomic rollback:
    # 1. No new attempt row inserted
    attempts = db_conn.execute("SELECT COUNT(*) FROM attempts").fetchone()[0]
    assert attempts == 0

    # 2. Mastery remains at 78.0, not updated to 82.4
    mastery = db_conn.execute("SELECT score FROM mastery WHERE student_id = ?", ("student-ananya",)).fetchone()[0]
    assert mastery == 78.0

    # 3. No notification persisted
    notifs = db_conn.execute("SELECT COUNT(*) FROM notifications").fetchone()[0]
    assert notifs == 0


# -------------------------------------------------------------------------
# 10. Atomic milestone persistence
# -------------------------------------------------------------------------
def test_atomic_milestone_persistence(db_conn):
    # Verify initial state is clean
    assert db_conn.execute("SELECT COUNT(*) FROM attempts").fetchone()[0] == 0
    assert db_conn.execute("SELECT COUNT(*) FROM mastery").fetchone()[0] == 0
    assert db_conn.execute("SELECT COUNT(*) FROM notifications").fetchone()[0] == 0

    # Seed mastery
    db_conn.execute(
        "INSERT INTO mastery (student_id, skill_id, score, last_practiced_at) VALUES (?, ?, ?, ?)",
        ("student-ananya", SKILL_A, 78.0, 1000.0),
    )

    res = record_attempt(db_conn, "student-ananya", SKILL_A, is_correct=True, now=1000.0)
    assert res.milestone_reached is True

    # Both mastery and notification committed
    assert db_conn.execute("SELECT score FROM mastery WHERE student_id = ?", ("student-ananya",)).fetchone()[0] == 82.4
    assert db_conn.execute("SELECT COUNT(*) FROM notifications").fetchone()[0] == 1
    assert db_conn.execute("SELECT COUNT(*) FROM attempts").fetchone()[0] == 1


# -------------------------------------------------------------------------
# 11. Same timestamp across all entities
# -------------------------------------------------------------------------
def test_same_timestamp_used_consistently(db_conn):
    fixed_now = 1710000000.123456

    db_conn.execute(
        "INSERT INTO mastery (student_id, skill_id, score, last_practiced_at) VALUES (?, ?, ?, ?)",
        ("student-ananya", SKILL_A, 78.0, fixed_now),
    )

    res = record_attempt(db_conn, "student-ananya", SKILL_A, is_correct=True, now=fixed_now)
    assert res.milestone_reached is True

    attempt_row = db_conn.execute("SELECT created_at FROM attempts").fetchone()
    mastery_row = db_conn.execute("SELECT last_practiced_at FROM mastery").fetchone()
    notif_row = db_conn.execute("SELECT reached_at FROM notifications").fetchone()

    assert attempt_row["created_at"] == fixed_now
    assert mastery_row["last_practiced_at"] == fixed_now
    assert notif_row["reached_at"] == fixed_now


# -------------------------------------------------------------------------
# 12. Cross-student isolation
# -------------------------------------------------------------------------
def test_cross_student_isolation(db_conn):
    now = 100000.0

    # Fill student-ananya to 30 attempts
    for i in range(30):
        db_conn.execute(
            "INSERT INTO attempts (student_id, skill_id, is_correct, created_at) VALUES (?, ?, ?, ?)",
            ("student-ananya", SKILL_A, 1, now - (i * 10)),
        )

    # Ananya is rate limited
    with pytest.raises(RateLimitExceeded):
        record_attempt(db_conn, "student-ananya", SKILL_A, is_correct=True, now=now)

    # Rohan has 0 attempts and can record successfully
    res_rohan = record_attempt(db_conn, "student-rohan", SKILL_A, is_correct=True, now=now)
    assert res_rohan.mastery == 60.0

    # Verify Rohan's mastery doesn't affect Ananya
    rohan_mastery = db_conn.execute("SELECT score FROM mastery WHERE student_id = ?", ("student-rohan",)).fetchone()[0]
    assert rohan_mastery == 60.0
    ananya_mastery = db_conn.execute("SELECT score FROM mastery WHERE student_id = ?", ("student-ananya",)).fetchone()
    assert ananya_mastery is None


# -------------------------------------------------------------------------
# 13. Concurrency / BEGIN IMMEDIATE invariant
# -------------------------------------------------------------------------
def test_begin_immediate_writer_reservation(tmp_path):
    """Verify that while Connection A holds its BEGIN IMMEDIATE transaction,
    Connection B cannot acquire its own BEGIN IMMEDIATE write lock until A commits.
    """
    db_file = str(tmp_path / "lock_test.db")
    init_db(db_file)

    conn_a = get_connection(db_file)
    conn_b = get_connection(db_file)
    conn_b.execute("PRAGMA busy_timeout = 0;")

    # Connection A begins immediate transaction
    conn_a.execute("BEGIN IMMEDIATE")

    # Connection B attempts BEGIN IMMEDIATE while A holds the write reservation
    with pytest.raises(sqlite3.OperationalError, match="database is locked"):
        conn_b.execute("BEGIN IMMEDIATE")

    # Once Connection A commits, Connection B can acquire BEGIN IMMEDIATE
    conn_a.execute("COMMIT")

    conn_b.execute("BEGIN IMMEDIATE")
    conn_b.execute("COMMIT")

    conn_a.close()
    conn_b.close()


# -------------------------------------------------------------------------
# 14. API Route: Valid attempt submission
# -------------------------------------------------------------------------
def test_api_submit_attempt_success(test_db_path, monkeypatch):
    monkeypatch.setattr(
        "mastery_service.main.get_ai_feedback",
        lambda skill_id, is_correct: f"Great job on {skill_id}!",
    )
    client = TestClient(app)
    resp = client.post(
        "/students/student-ananya/attempts",
        headers={"Authorization": "Bearer token-student-ananya"},
        json={"skill_id": SKILL_A, "is_correct": True},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["skill_id"] == SKILL_A
    assert data["mastery"] == 60.0
    assert data["milestone_reached"] is False
    assert data["feedback"] == f"Great job on {SKILL_A}!"
    assert data["feedback_status"] == "ok"


# -------------------------------------------------------------------------
# 15. API Route: Access control enforcement
# -------------------------------------------------------------------------
def test_api_access_control(test_db_path):
    client = TestClient(app)

    # Teacher Kavita attempting to submit for Ananya -> 404
    resp_teacher = client.post(
        "/students/student-ananya/attempts",
        headers={"Authorization": "Bearer token-teacher-kavita"},
        json={"skill_id": SKILL_A, "is_correct": True},
    )
    assert resp_teacher.status_code == 404

    # Rohan attempting to submit for Ananya -> 404
    resp_rohan = client.post(
        "/students/student-ananya/attempts",
        headers={"Authorization": "Bearer token-student-rohan"},
        json={"skill_id": SKILL_A, "is_correct": True},
    )
    assert resp_rohan.status_code == 404

    # Unknown token -> 401
    resp_unknown = client.post(
        "/students/student-ananya/attempts",
        headers={"Authorization": "Bearer not-a-token"},
        json={"skill_id": SKILL_A, "is_correct": True},
    )
    assert resp_unknown.status_code == 401


# -------------------------------------------------------------------------
# 16. API Route: Invalid request does not consume rate limit capacity
# -------------------------------------------------------------------------
def test_api_invalid_request_does_not_consume_rate_limit(test_db_path):
    client = TestClient(app)

    # Invalid skill_id -> 422
    resp = client.post(
        "/students/student-ananya/attempts",
        headers={"Authorization": "Bearer token-student-ananya"},
        json={"skill_id": "math.fractions.non-existent", "is_correct": True},
    )
    assert resp.status_code == 422

    # Malformed body -> 422
    resp_malformed = client.post(
        "/students/student-ananya/attempts",
        headers={"Authorization": "Bearer token-student-ananya"},
        json={"skill_id": SKILL_A},
    )
    assert resp_malformed.status_code == 422

    # Verify no attempts were recorded
    conn = get_connection(test_db_path)
    count = conn.execute("SELECT COUNT(*) FROM attempts").fetchone()[0]
    conn.close()
    assert count == 0


# -------------------------------------------------------------------------
# 17. API Route: Rate limit exceeded (429 + Retry-After)
# -------------------------------------------------------------------------
def test_api_rate_limit_exceeded(test_db_path):
    client = TestClient(app)

    conn = get_connection(test_db_path)
    now = time.time()
    # Insert 30 attempts for student-ananya within rolling 24h
    for i in range(30):
        conn.execute(
            "INSERT INTO attempts (student_id, skill_id, is_correct, created_at) VALUES (?, ?, ?, ?)",
            ("student-ananya", SKILL_A, 1, now - (i * 10)),
        )
    conn.close()

    resp = client.post(
        "/students/student-ananya/attempts",
        headers={"Authorization": "Bearer token-student-ananya"},
        json={"skill_id": SKILL_A, "is_correct": True},
    )
    assert resp.status_code == 429
    assert "Retry-After" in resp.headers
    data = resp.json()
    assert data["detail"] == "Rate limit exceeded"
    assert "retry_after_seconds" in data
    assert data["retry_after_seconds"] >= 0


# =========================================================================
# AI feedback tests (Commit 6)
# =========================================================================


# -------------------------------------------------------------------------
# 18. AI success → feedback returned with status "ok"
# -------------------------------------------------------------------------
def test_ai_success(test_db_path, monkeypatch):
    monkeypatch.setattr(
        "mastery_service.main.get_ai_feedback",
        lambda skill_id, is_correct: f"Great job on {skill_id}!",
    )
    client = TestClient(app)
    resp = client.post(
        "/students/student-ananya/attempts",
        headers={"Authorization": "Bearer token-student-ananya"},
        json={"skill_id": SKILL_A, "is_correct": True},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["feedback"] == f"Great job on {SKILL_A}!"
    assert data["feedback_status"] == "ok"
    assert data["mastery"] == 60.0
    assert data["milestone_reached"] is False

    # Verify persistence
    conn = get_connection(test_db_path)
    assert conn.execute("SELECT COUNT(*) FROM attempts").fetchone()[0] == 1
    assert conn.execute("SELECT score FROM mastery WHERE student_id = 'student-ananya'").fetchone()[0] == 60.0
    conn.close()


# -------------------------------------------------------------------------
# 19. AI provider exception → attempt persisted, feedback unavailable
# -------------------------------------------------------------------------
def test_ai_exception_still_succeeds(test_db_path, monkeypatch):
    def failing_provider(skill_id, is_correct):
        raise RuntimeError("AI provider exploded")

    monkeypatch.setattr("mastery_service.main.get_ai_feedback", failing_provider)
    client = TestClient(app)
    resp = client.post(
        "/students/student-ananya/attempts",
        headers={"Authorization": "Bearer token-student-ananya"},
        json={"skill_id": SKILL_A, "is_correct": True},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["feedback"] is None
    assert data["feedback_status"] == "unavailable"
    assert data["mastery"] == 60.0

    conn = get_connection(test_db_path)
    assert conn.execute("SELECT COUNT(*) FROM attempts").fetchone()[0] == 1
    assert conn.execute("SELECT score FROM mastery WHERE student_id = 'student-ananya'").fetchone()[0] == 60.0
    conn.close()


# -------------------------------------------------------------------------
# 20. AI timeout → attempt persisted, feedback unavailable
# -------------------------------------------------------------------------
def test_ai_timeout_still_succeeds(test_db_path, monkeypatch):
    import threading

    def slow_provider(skill_id, is_correct):
        # Block long enough to exceed the 6-second timeout, but use an event
        # so the thread can be interrupted quickly when the timeout fires.
        threading.Event().wait(timeout=1)
        return "should never arrive"

    monkeypatch.setattr("mastery_service.main.get_ai_feedback", slow_provider)
    # Shorten the timeout so the test doesn't actually wait 6 seconds
    monkeypatch.setattr("mastery_service.main.AI_TIMEOUT_SECONDS", 0.1)

    client = TestClient(app)
    resp = client.post(
        "/students/student-ananya/attempts",
        headers={"Authorization": "Bearer token-student-ananya"},
        json={"skill_id": SKILL_A, "is_correct": True},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["feedback"] is None
    assert data["feedback_status"] == "unavailable"

    conn = get_connection(test_db_path)
    assert conn.execute("SELECT COUNT(*) FROM attempts").fetchone()[0] == 1
    conn.close()


# -------------------------------------------------------------------------
# 21. AI is called only after successful persistence
# -------------------------------------------------------------------------
def test_ai_called_after_persistence(test_db_path, monkeypatch):
    call_log = []

    def inspecting_provider(skill_id, is_correct):
        # At the moment the AI provider is invoked, verify the attempt is
        # already committed to the database.
        conn = get_connection(test_db_path)
        count = conn.execute("SELECT COUNT(*) FROM attempts WHERE student_id = 'student-ananya'").fetchone()[0]
        conn.close()
        call_log.append({"count_at_call_time": count})
        return "feedback"

    monkeypatch.setattr("mastery_service.main.get_ai_feedback", inspecting_provider)
    client = TestClient(app)
    resp = client.post(
        "/students/student-ananya/attempts",
        headers={"Authorization": "Bearer token-student-ananya"},
        json={"skill_id": SKILL_A, "is_correct": True},
    )
    assert resp.status_code == 200
    assert len(call_log) == 1
    assert call_log[0]["count_at_call_time"] == 1


# -------------------------------------------------------------------------
# 22. Rate-limit rejection does not call AI
# -------------------------------------------------------------------------
def test_rate_limited_does_not_call_ai(test_db_path, monkeypatch):
    call_count = {"n": 0}

    def counting_provider(skill_id, is_correct):
        call_count["n"] += 1
        return "feedback"

    monkeypatch.setattr("mastery_service.main.get_ai_feedback", counting_provider)

    conn = get_connection(test_db_path)
    now = time.time()
    for i in range(30):
        conn.execute(
            "INSERT INTO attempts (student_id, skill_id, is_correct, created_at) VALUES (?, ?, ?, ?)",
            ("student-ananya", SKILL_A, 1, now - (i * 10)),
        )
    conn.close()

    client = TestClient(app)
    resp = client.post(
        "/students/student-ananya/attempts",
        headers={"Authorization": "Bearer token-student-ananya"},
        json={"skill_id": SKILL_A, "is_correct": True},
    )
    assert resp.status_code == 429
    assert call_count["n"] == 0


# -------------------------------------------------------------------------
# 23. Invalid request does not call AI
# -------------------------------------------------------------------------
def test_invalid_request_does_not_call_ai(test_db_path, monkeypatch):
    call_count = {"n": 0}

    def counting_provider(skill_id, is_correct):
        call_count["n"] += 1
        return "feedback"

    monkeypatch.setattr("mastery_service.main.get_ai_feedback", counting_provider)

    client = TestClient(app)
    resp = client.post(
        "/students/student-ananya/attempts",
        headers={"Authorization": "Bearer token-student-ananya"},
        json={"skill_id": "nonexistent-skill", "is_correct": True},
    )
    assert resp.status_code == 422
    assert call_count["n"] == 0


# -------------------------------------------------------------------------
# 24. Unauthorized submission does not call AI
# -------------------------------------------------------------------------
def test_unauthorized_does_not_call_ai(test_db_path, monkeypatch):
    call_count = {"n": 0}

    def counting_provider(skill_id, is_correct):
        call_count["n"] += 1
        return "feedback"

    monkeypatch.setattr("mastery_service.main.get_ai_feedback", counting_provider)

    client = TestClient(app)
    # Another student trying to submit for ananya
    resp = client.post(
        "/students/student-ananya/attempts",
        headers={"Authorization": "Bearer token-student-rohan"},
        json={"skill_id": SKILL_A, "is_correct": True},
    )
    assert resp.status_code == 404
    assert call_count["n"] == 0


# -------------------------------------------------------------------------
# 25. Domain/database failure does not call AI
# -------------------------------------------------------------------------
def test_domain_failure_does_not_call_ai(test_db_path, monkeypatch):
    call_count = {"n": 0}

    def counting_provider(skill_id, is_correct):
        call_count["n"] += 1
        return "feedback"

    monkeypatch.setattr("mastery_service.main.get_ai_feedback", counting_provider)

    def exploding_record(*args, **kwargs):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr("mastery_service.main.record_attempt", exploding_record)

    client = TestClient(app, raise_server_exceptions=False)
    resp = client.post(
        "/students/student-ananya/attempts",
        headers={"Authorization": "Bearer token-student-ananya"},
        json={"skill_id": SKILL_A, "is_correct": True},
    )
    assert resp.status_code == 500
    assert call_count["n"] == 0


# -------------------------------------------------------------------------
# 26. AI does not affect milestone durability
# -------------------------------------------------------------------------
def test_milestone_durable_despite_ai_failure(test_db_path, monkeypatch):
    def failing_provider(skill_id, is_correct):
        raise RuntimeError("AI down")

    monkeypatch.setattr("mastery_service.main.get_ai_feedback", failing_provider)

    # Seed mastery at 78.0 to trigger milestone crossing
    conn = get_connection(test_db_path)
    conn.execute(
        "INSERT INTO mastery (student_id, skill_id, score, last_practiced_at) VALUES (?, ?, ?, ?)",
        ("student-ananya", SKILL_A, 78.0, time.time()),
    )
    conn.close()

    client = TestClient(app)
    resp = client.post(
        "/students/student-ananya/attempts",
        headers={"Authorization": "Bearer token-student-ananya"},
        json={"skill_id": SKILL_A, "is_correct": True},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["milestone_reached"] is True
    assert data["feedback"] is None
    assert data["feedback_status"] == "unavailable"

    # Milestone notification is durable
    conn = get_connection(test_db_path)
    notifs = conn.execute(
        "SELECT COUNT(*) FROM notifications WHERE student_id = 'student-ananya' AND skill_id = ?",
        (SKILL_A,),
    ).fetchone()[0]
    conn.close()
    assert notifs == 1


# -------------------------------------------------------------------------
# 27. No retry — exactly one AI invocation on failure
# -------------------------------------------------------------------------
def test_no_retry_on_ai_failure(test_db_path, monkeypatch):
    call_count = {"n": 0}

    def failing_provider(skill_id, is_correct):
        call_count["n"] += 1
        raise RuntimeError("transient failure")

    monkeypatch.setattr("mastery_service.main.get_ai_feedback", failing_provider)

    client = TestClient(app)
    resp = client.post(
        "/students/student-ananya/attempts",
        headers={"Authorization": "Bearer token-student-ananya"},
        json={"skill_id": SKILL_A, "is_correct": True},
    )
    assert resp.status_code == 200
    assert call_count["n"] == 1

