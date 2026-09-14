"""Tests for Idempotency-Key support on POST /students/{student_id}/attempts."""

import concurrent.futures
import os
import sqlite3
import time
import pytest
from fastapi.testclient import TestClient

from mastery_service.attempts import (
    AttemptResult,
    IdempotencyConflict,
    RateLimitExceeded,
    record_attempt,
)
from mastery_service.db import SCHEMA_SQL, get_connection, init_db
from mastery_service.main import app
from mastery_service.seed_data import SKILL_IDS

SKILL_A = SKILL_IDS[0]
SKILL_B = SKILL_IDS[1]


@pytest.fixture(autouse=True)
def fast_ai_feedback(monkeypatch):
    """Fast, deterministic AI feedback mock for test suite."""
    monkeypatch.setattr(
        "mastery_service.main.get_ai_feedback",
        lambda skill_id, is_correct: f"Great job on {skill_id}!",
    )


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
    db_file = str(tmp_path / "test_idempotency.db")
    monkeypatch.setenv("MASTERY_DB_PATH", db_file)
    init_db(db_file)
    yield db_file
    if os.path.exists(db_file):
        try:
            os.remove(db_file)
        except OSError:
            pass


# -------------------------------------------------------------------------
# 1. Domain-level tests: record_attempt() replay and conflict
# -------------------------------------------------------------------------
def test_domain_record_attempt_replay(db_conn):
    res1 = record_attempt(
        db_conn,
        student_id="student-ananya",
        skill_id=SKILL_A,
        is_correct=True,
        now=1000.0,
        idempotency_key="key-domain-1",
    )
    assert res1.skill_id == SKILL_A
    assert res1.mastery == 60.0
    assert res1.milestone_reached is False
    assert res1.is_replayed is False

    # Second call with identical parameters
    res2 = record_attempt(
        db_conn,
        student_id="student-ananya",
        skill_id=SKILL_A,
        is_correct=True,
        now=1010.0,
        idempotency_key="key-domain-1",
    )
    assert res2.skill_id == SKILL_A
    assert res2.mastery == 60.0
    assert res2.milestone_reached is False
    assert res2.is_replayed is True

    # Check attempts table: only 1 attempt recorded
    cur = db_conn.execute("SELECT * FROM attempts WHERE student_id = ?", ("student-ananya",))
    rows = cur.fetchall()
    assert len(rows) == 1
    assert rows[0]["created_at"] == 1000.0


def test_domain_record_attempt_conflict_skill(db_conn):
    record_attempt(
        db_conn,
        student_id="student-ananya",
        skill_id=SKILL_A,
        is_correct=True,
        now=1000.0,
        idempotency_key="key-domain-conflict",
    )

    with pytest.raises(IdempotencyConflict) as exc_info:
        record_attempt(
            db_conn,
            student_id="student-ananya",
            skill_id=SKILL_B,
            is_correct=True,
            now=1010.0,
            idempotency_key="key-domain-conflict",
        )

    assert exc_info.value.detail == "Idempotency key already used with different attempt parameters"

    # Mastery should only have SKILL_A
    cur = db_conn.execute("SELECT skill_id FROM mastery WHERE student_id = ?", ("student-ananya",))
    rows = cur.fetchall()
    assert len(rows) == 1
    assert rows[0]["skill_id"] == SKILL_A


def test_domain_record_attempt_conflict_is_correct(db_conn):
    record_attempt(
        db_conn,
        student_id="student-ananya",
        skill_id=SKILL_A,
        is_correct=True,
        now=1000.0,
        idempotency_key="key-domain-conflict-correct",
    )

    with pytest.raises(IdempotencyConflict) as exc_info:
        record_attempt(
            db_conn,
            student_id="student-ananya",
            skill_id=SKILL_A,
            is_correct=False,
            now=1010.0,
            idempotency_key="key-domain-conflict-correct",
        )

    assert exc_info.value.detail == "Idempotency key already used with different attempt parameters"


# -------------------------------------------------------------------------
# 2. Core API Test 1: Identical replay returns identical stored result
# -------------------------------------------------------------------------
def test_api_identical_replay_returns_stored_result(test_db_path):
    client = TestClient(app)
    headers = {
        "Authorization": "Bearer token-student-ananya",
        "Idempotency-Key": "req-ananya-001",
    }
    payload = {"skill_id": SKILL_A, "is_correct": True}

    resp1 = client.post("/students/student-ananya/attempts", headers=headers, json=payload)
    assert resp1.status_code == 200
    data1 = resp1.json()
    assert data1["skill_id"] == SKILL_A
    assert data1["mastery"] == 60.0
    assert data1["milestone_reached"] is False
    assert data1["feedback"] == f"Great job on {SKILL_A}!"
    assert data1["feedback_status"] == "ok"

    # Second call with identical key and payload
    resp2 = client.post("/students/student-ananya/attempts", headers=headers, json=payload)
    assert resp2.status_code == 200
    data2 = resp2.json()
    assert data2["skill_id"] == SKILL_A
    assert data2["mastery"] == 60.0
    assert data2["milestone_reached"] is False
    # Replay should NOT call AI feedback: feedback=None, feedback_status="unavailable"
    assert data2["feedback"] is None
    assert data2["feedback_status"] == "unavailable"

    # Verify only ONE attempt recorded in SQLite
    conn = get_connection(test_db_path)
    attempts = conn.execute("SELECT * FROM attempts WHERE student_id = ?", ("student-ananya",)).fetchall()
    assert len(attempts) == 1

    idempotency_rows = conn.execute(
        "SELECT * FROM idempotency_keys WHERE student_id = ?", ("student-ananya",)
    ).fetchall()
    assert len(idempotency_rows) == 1
    assert idempotency_rows[0]["idempotency_key"] == "req-ananya-001"
    assert idempotency_rows[0]["skill_id"] == SKILL_A
    assert idempotency_rows[0]["is_correct"] == 1

    mastery_rows = conn.execute("SELECT * FROM mastery WHERE student_id = ?", ("student-ananya",)).fetchall()
    assert len(mastery_rows) == 1
    assert mastery_rows[0]["score"] == 60.0

    notif_rows = conn.execute("SELECT * FROM notifications WHERE student_id = ?", ("student-ananya",)).fetchall()
    assert len(notif_rows) == 0
    conn.close()


def test_api_identical_replay_when_ai_feedback_unavailable(test_db_path, monkeypatch):
    """When AI feedback fails or is unavailable on initial call, replay response body is identical."""
    monkeypatch.setattr(
        "mastery_service.main.get_ai_feedback",
        lambda skill_id, is_correct: (_ for _ in ()).throw(RuntimeError("AI offline")),
    )
    client = TestClient(app)
    headers = {
        "Authorization": "Bearer token-student-ananya",
        "Idempotency-Key": "req-ananya-no-ai",
    }
    payload = {"skill_id": SKILL_A, "is_correct": True}

    resp1 = client.post("/students/student-ananya/attempts", headers=headers, json=payload)
    resp2 = client.post("/students/student-ananya/attempts", headers=headers, json=payload)

    assert resp1.status_code == 200
    assert resp2.status_code == 200
    assert resp1.json() == resp2.json()
    assert resp2.json()["feedback"] is None
    assert resp2.json()["feedback_status"] == "unavailable"


def test_api_identical_replay_with_milestone(test_db_path):
    # Prime student to 78.0 to cross 80.0 threshold
    conn = get_connection(test_db_path)
    conn.execute(
        "INSERT INTO mastery (student_id, skill_id, score, last_practiced_at) VALUES (?, ?, ?, ?)",
        ("student-ananya", SKILL_A, 78.0, time.time()),
    )
    conn.close()

    client = TestClient(app)
    headers = {
        "Authorization": "Bearer token-student-ananya",
        "Idempotency-Key": "milestone-key",
    }
    payload = {"skill_id": SKILL_A, "is_correct": True}
    resp1 = client.post("/students/student-ananya/attempts", headers=headers, json=payload)
    assert resp1.status_code == 200
    assert resp1.json()["milestone_reached"] is True

    # Replay the milestone-crossing attempt
    resp2 = client.post("/students/student-ananya/attempts", headers=headers, json=payload)
    assert resp2.status_code == 200
    assert resp2.json()["milestone_reached"] is True

    # Verify notifications table has exactly ONE notification row
    conn = get_connection(test_db_path)
    notif_rows = conn.execute("SELECT * FROM notifications WHERE student_id = ?", ("student-ananya",)).fetchall()
    assert len(notif_rows) == 1
    conn.close()


# -------------------------------------------------------------------------
# 3. Core API Test 2: Conflict on differing attempt parameters -> HTTP 409
# -------------------------------------------------------------------------
def test_api_conflict_different_skill(test_db_path):
    client = TestClient(app)
    headers = {
        "Authorization": "Bearer token-student-ananya",
        "Idempotency-Key": "conflict-key-1",
    }

    # First attempt with SKILL_A
    resp1 = client.post(
        "/students/student-ananya/attempts",
        headers=headers,
        json={"skill_id": SKILL_A, "is_correct": True},
    )
    assert resp1.status_code == 200

    # Second attempt with same key but SKILL_B
    resp2 = client.post(
        "/students/student-ananya/attempts",
        headers=headers,
        json={"skill_id": SKILL_B, "is_correct": True},
    )
    assert resp2.status_code == 409
    assert resp2.json() == {"detail": "Idempotency key already used with different attempt parameters"}

    # Verify database state was not mutated by the second call
    conn = get_connection(test_db_path)
    attempts = conn.execute("SELECT * FROM attempts WHERE student_id = ?", ("student-ananya",)).fetchall()
    assert len(attempts) == 1
    assert attempts[0]["skill_id"] == SKILL_A

    mastery_rows = conn.execute("SELECT * FROM mastery WHERE student_id = ?", ("student-ananya",)).fetchall()
    assert len(mastery_rows) == 1
    assert mastery_rows[0]["skill_id"] == SKILL_A
    conn.close()


def test_api_conflict_different_is_correct(test_db_path):
    client = TestClient(app)
    headers = {
        "Authorization": "Bearer token-student-ananya",
        "Idempotency-Key": "conflict-key-2",
    }

    # First attempt is_correct=True
    resp1 = client.post(
        "/students/student-ananya/attempts",
        headers=headers,
        json={"skill_id": SKILL_A, "is_correct": True},
    )
    assert resp1.status_code == 200

    # Second attempt is_correct=False with same key
    resp2 = client.post(
        "/students/student-ananya/attempts",
        headers=headers,
        json={"skill_id": SKILL_A, "is_correct": False},
    )
    assert resp2.status_code == 409
    assert resp2.json() == {"detail": "Idempotency key already used with different attempt parameters"}

    # Verify database state: only first attempt recorded, score is 60.0
    conn = get_connection(test_db_path)
    attempts = conn.execute("SELECT * FROM attempts WHERE student_id = ?", ("student-ananya",)).fetchall()
    assert len(attempts) == 1
    assert attempts[0]["is_correct"] == 1

    mastery = conn.execute("SELECT score FROM mastery WHERE student_id = ?", ("student-ananya",)).fetchone()
    assert mastery["score"] == 60.0
    conn.close()


# -------------------------------------------------------------------------
# 4. Core API Test 3: Replaying does NOT consume rate-limit capacity
# -------------------------------------------------------------------------
def test_api_replay_does_not_consume_rate_limit(test_db_path):
    client = TestClient(app)
    headers_base = {"Authorization": "Bearer token-student-ananya"}

    # Initial attempt with idempotency key
    resp = client.post(
        "/students/student-ananya/attempts",
        headers={**headers_base, "Idempotency-Key": "key-for-replay-test"},
        json={"skill_id": SKILL_A, "is_correct": True},
    )
    assert resp.status_code == 200

    # Replay the same key 5 times
    for _ in range(5):
        resp_replay = client.post(
            "/students/student-ananya/attempts",
            headers={**headers_base, "Idempotency-Key": "key-for-replay-test"},
            json={"skill_id": SKILL_A, "is_correct": True},
        )
        assert resp_replay.status_code == 200

    # The student has only used 1 attempt slot.
    # They should be able to make 29 NEW attempts (total 30) without hitting rate limit.
    for i in range(29):
        resp_new = client.post(
            "/students/student-ananya/attempts",
            headers={**headers_base, "Idempotency-Key": f"new-key-{i}"},
            json={"skill_id": SKILL_A, "is_correct": True},
        )
        assert resp_new.status_code == 200, f"Attempt {i+2} failed: {resp_new.text}"

    # 31st new attempt should be rate limited (429)
    resp_limit = client.post(
        "/students/student-ananya/attempts",
        headers={**headers_base, "Idempotency-Key": "attempt-31"},
        json={"skill_id": SKILL_A, "is_correct": True},
    )
    assert resp_limit.status_code == 429
    assert "Rate limit exceeded" in resp_limit.json()["detail"]

    # Even while rate limited, replaying the original key STILL succeeds!
    resp_replay_while_limited = client.post(
        "/students/student-ananya/attempts",
        headers={**headers_base, "Idempotency-Key": "key-for-replay-test"},
        json={"skill_id": SKILL_A, "is_correct": True},
    )
    assert resp_replay_while_limited.status_code == 200


# -------------------------------------------------------------------------
# 5. Core API Test 4: Omitted Idempotency-Key header preserves existing behavior
# -------------------------------------------------------------------------
def test_api_omitted_idempotency_key_header(test_db_path):
    client = TestClient(app)
    headers = {"Authorization": "Bearer token-student-ananya"}

    # Call twice without Idempotency-Key
    resp1 = client.post(
        "/students/student-ananya/attempts",
        headers=headers,
        json={"skill_id": SKILL_A, "is_correct": True},
    )
    assert resp1.status_code == 200
    assert resp1.json()["mastery"] == 60.0

    resp2 = client.post(
        "/students/student-ananya/attempts",
        headers=headers,
        json={"skill_id": SKILL_A, "is_correct": True},
    )
    assert resp2.status_code == 200
    assert resp2.json()["mastery"] == pytest.approx(68.0, rel=1e-3)

    conn = get_connection(test_db_path)
    attempts = conn.execute("SELECT * FROM attempts WHERE student_id = ?", ("student-ananya",)).fetchall()
    assert len(attempts) == 2

    # No rows inserted into idempotency_keys table
    idempotency_rows = conn.execute("SELECT * FROM idempotency_keys").fetchall()
    assert len(idempotency_rows) == 0
    conn.close()


# -------------------------------------------------------------------------
# 6. Core API Test 5: Scoped per student — same key string across two students
# -------------------------------------------------------------------------
def test_api_scoped_per_student_same_key(test_db_path):
    client = TestClient(app)
    shared_key = "common-client-assigned-uuid"

    # Student Ananya uses shared_key
    resp_ananya = client.post(
        "/students/student-ananya/attempts",
        headers={
            "Authorization": "Bearer token-student-ananya",
            "Idempotency-Key": shared_key,
        },
        json={"skill_id": SKILL_A, "is_correct": True},
    )
    assert resp_ananya.status_code == 200

    # Student Rohan uses the exact same shared_key (even with a different skill!)
    resp_rohan = client.post(
        "/students/student-rohan/attempts",
        headers={
            "Authorization": "Bearer token-student-rohan",
            "Idempotency-Key": shared_key,
        },
        json={"skill_id": SKILL_B, "is_correct": False},
    )
    assert resp_rohan.status_code == 200

    conn = get_connection(test_db_path)
    rows = conn.execute("SELECT student_id, idempotency_key, skill_id FROM idempotency_keys").fetchall()
    assert len(rows) == 2

    students_with_key = {r["student_id"]: r["skill_id"] for r in rows}
    assert students_with_key["student-ananya"] == SKILL_A
    assert students_with_key["student-rohan"] == SKILL_B
    conn.close()


# -------------------------------------------------------------------------
# 7. Stretch Test: Concurrent requests with same idempotency key
# -------------------------------------------------------------------------
def test_api_concurrent_requests_same_key(test_db_path):
    key = "concurrent-idempotent-key"

    def submit():
        c = TestClient(app)
        return c.post(
            "/students/student-ananya/attempts",
            headers={
                "Authorization": "Bearer token-student-ananya",
                "Idempotency-Key": key,
            },
            json={"skill_id": SKILL_A, "is_correct": True},
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        f1 = executor.submit(submit)
        f2 = executor.submit(submit)
        resp1 = f1.result()
        resp2 = f2.result()

    assert resp1.status_code == 200
    assert resp2.status_code == 200

    assert resp1.json()["skill_id"] == SKILL_A
    assert resp2.json()["skill_id"] == SKILL_A
    assert resp1.json()["mastery"] == 60.0
    assert resp2.json()["mastery"] == 60.0

    conn = get_connection(test_db_path)
    # Exactly one attempt recorded in DB
    attempts = conn.execute("SELECT * FROM attempts WHERE student_id = ?", ("student-ananya",)).fetchall()
    assert len(attempts) == 1

    # Exactly one idempotency_keys row recorded in DB
    idempotency_rows = conn.execute(
        "SELECT * FROM idempotency_keys WHERE student_id = ?", ("student-ananya",)
    ).fetchall()
    assert len(idempotency_rows) == 1
    conn.close()
