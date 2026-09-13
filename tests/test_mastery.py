"""Tests for GET /students/{student_id}/mastery with live decay at read time."""

import os
import sqlite3
import time
import pytest
from fastapi.testclient import TestClient

from mastery_service.db import get_connection, init_db
from mastery_service.main import app
from mastery_service.scoring import BASELINE_SEED, HALF_LIFE_DAYS, SECONDS_PER_DAY
from mastery_service.seed_data import SKILL_IDS, TOKENS


@pytest.fixture
def test_db_path(tmp_path, monkeypatch):
    """File-based SQLite database for API tests."""
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
# 1. Student can GET their own mastery -> 200
# -------------------------------------------------------------------------
def test_student_can_get_own_mastery(test_db_path):
    client = TestClient(app)
    resp = client.get(
        "/students/student-ananya/mastery",
        headers={"Authorization": "Bearer token-student-ananya"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["student_id"] == "student-ananya"
    assert isinstance(data["mastery"], list)
    assert len(data["mastery"]) == len(SKILL_IDS)


# -------------------------------------------------------------------------
# 2. Teacher can GET a student on their roster -> 200
# -------------------------------------------------------------------------
def test_teacher_can_get_rostered_student_mastery(test_db_path):
    client = TestClient(app)
    # Teacher Kavita has student-ananya and student-rohan on her roster
    resp = client.get(
        "/students/student-ananya/mastery",
        headers={"Authorization": "Bearer token-teacher-kavita"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["student_id"] == "student-ananya"
    assert len(data["mastery"]) == len(SKILL_IDS)


# -------------------------------------------------------------------------
# 3. Student cannot GET another student's mastery -> 404
# -------------------------------------------------------------------------
def test_student_cannot_get_other_student_mastery(test_db_path):
    client = TestClient(app)
    # Rohan trying to view Ananya's mastery
    resp = client.get(
        "/students/student-ananya/mastery",
        headers={"Authorization": "Bearer token-student-rohan"},
    )
    assert resp.status_code == 404
    assert resp.json()["detail"] == "Not found"


# -------------------------------------------------------------------------
# 4. Teacher cannot GET a student outside their roster -> 404
# -------------------------------------------------------------------------
def test_teacher_cannot_get_unrostered_student_mastery(test_db_path):
    client = TestClient(app)
    # Mei is not on Kavita's roster
    resp = client.get(
        "/students/student-mei/mastery",
        headers={"Authorization": "Bearer token-teacher-kavita"},
    )
    assert resp.status_code == 404
    assert resp.json()["detail"] == "Not found"


# -------------------------------------------------------------------------
# 5. Unknown/missing token -> 401
# -------------------------------------------------------------------------
def test_missing_or_unknown_token_returns_401(test_db_path):
    client = TestClient(app)

    # Missing authorization header
    resp_missing = client.get("/students/student-ananya/mastery")
    assert resp_missing.status_code == 401
    assert resp_missing.json()["detail"] == "Invalid or missing token"

    # Empty token
    resp_empty = client.get(
        "/students/student-ananya/mastery",
        headers={"Authorization": "Bearer "},
    )
    assert resp_empty.status_code == 401

    # Unknown token
    resp_unknown = client.get(
        "/students/student-ananya/mastery",
        headers={"Authorization": "Bearer not-a-valid-token"},
    )
    assert resp_unknown.status_code == 401
    assert resp_unknown.json()["detail"] == "Invalid or missing token"


# -------------------------------------------------------------------------
# 6. Missing mastery rows return all skills at 50.0
# -------------------------------------------------------------------------
def test_missing_mastery_rows_return_all_skills_at_baseline(test_db_path):
    client = TestClient(app)
    resp = client.get(
        "/students/student-ananya/mastery",
        headers={"Authorization": "Bearer token-student-ananya"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["student_id"] == "student-ananya"
    assert len(data["mastery"]) == len(SKILL_IDS)

    for item in data["mastery"]:
        assert item["skill_id"] in SKILL_IDS
        assert item["mastery"] == BASELINE_SEED


# -------------------------------------------------------------------------
# 7. Existing mastery is decayed at read time
# -------------------------------------------------------------------------
def test_existing_mastery_decayed_at_read_time(test_db_path, monkeypatch):
    t0 = 1000000.0
    half_life_seconds = HALF_LIFE_DAYS * SECONDS_PER_DAY
    t_read = t0 + half_life_seconds  # exactly 1 half-life (30 days) later

    # Seed stored mastery at score = 80.0 at t0
    conn = get_connection(test_db_path)
    conn.execute(
        "INSERT INTO mastery (student_id, skill_id, score, last_practiced_at) VALUES (?, ?, ?, ?)",
        ("student-ananya", SKILL_IDS[0], 80.0, t0),
    )
    conn.close()

    monkeypatch.setattr("time.time", lambda: t_read)

    client = TestClient(app)
    resp = client.get(
        "/students/student-ananya/mastery",
        headers={"Authorization": "Bearer token-student-ananya"},
    )
    assert resp.status_code == 200
    data = resp.json()

    mastery_map = {item["skill_id"]: item["mastery"] for item in data["mastery"]}

    # Stored 80.0 decayed by 1 half-life -> 40.0
    assert mastery_map[SKILL_IDS[0]] == pytest.approx(40.0, rel=1e-5)

    # Unpracticed skills remain at 50.0 baseline
    for skill_id in SKILL_IDS[1:]:
        assert mastery_map[skill_id] == BASELINE_SEED


# -------------------------------------------------------------------------
# 8, 9, 10. GET does not mutate mastery table, attempts, or notifications
# -------------------------------------------------------------------------
def test_get_mastery_is_side_effect_free(test_db_path, monkeypatch):
    t0 = 1000000.0
    conn = get_connection(test_db_path)
    # Seed 1 attempt, 1 mastery row, and 1 notification
    conn.execute(
        "INSERT INTO attempts (student_id, skill_id, is_correct, created_at) VALUES (?, ?, ?, ?)",
        ("student-ananya", SKILL_IDS[0], 1, t0),
    )
    conn.execute(
        "INSERT INTO mastery (student_id, skill_id, score, last_practiced_at) VALUES (?, ?, ?, ?)",
        ("student-ananya", SKILL_IDS[0], 85.0, t0),
    )
    conn.execute(
        "INSERT INTO notifications (student_id, skill_id, reached_at) VALUES (?, ?, ?)",
        ("student-ananya", SKILL_IDS[0], t0),
    )

    # Snapshot tables before GET
    mastery_before = conn.execute("SELECT * FROM mastery ORDER BY student_id, skill_id").fetchall()
    attempts_before = conn.execute("SELECT * FROM attempts ORDER BY id").fetchall()
    notifs_before = conn.execute("SELECT * FROM notifications ORDER BY id").fetchall()
    conn.close()

    # Read with live decay
    monkeypatch.setattr("time.time", lambda: t0 + (HALF_LIFE_DAYS * SECONDS_PER_DAY))
    client = TestClient(app)
    resp = client.get(
        "/students/student-ananya/mastery",
        headers={"Authorization": "Bearer token-student-ananya"},
    )
    assert resp.status_code == 200

    # Snapshot tables after GET
    conn_after = get_connection(test_db_path)
    mastery_after = conn_after.execute("SELECT * FROM mastery ORDER BY student_id, skill_id").fetchall()
    attempts_after = conn_after.execute("SELECT * FROM attempts ORDER BY id").fetchall()
    notifs_after = conn_after.execute("SELECT * FROM notifications ORDER BY id").fetchall()
    conn_after.close()

    # 8. Mastery table is completely unchanged (score remains stored 85.0, last_practiced_at remains t0)
    assert len(mastery_after) == len(mastery_before)
    for row_b, row_a in zip(mastery_before, mastery_after):
        assert dict(row_b) == dict(row_a)
    assert mastery_after[0]["score"] == 85.0
    assert mastery_after[0]["last_practiced_at"] == t0

    # 9. Attempts count and rows unchanged
    assert len(attempts_after) == len(attempts_before)
    for row_b, row_a in zip(attempts_before, attempts_after):
        assert dict(row_b) == dict(row_a)

    # 10. Notifications count and rows unchanged
    assert len(notifs_after) == len(notifs_before)
    for row_b, row_a in zip(notifs_before, notifs_after):
        assert dict(row_b) == dict(row_a)


# -------------------------------------------------------------------------
# 11. All skills use the same captured now
# -------------------------------------------------------------------------
def test_all_skills_use_same_captured_now(test_db_path, monkeypatch):
    """Verify that time.time() is captured once per request, ensuring uniform decay calculation."""
    # Seed all skills with stored scores
    t0 = 1000000.0
    conn = get_connection(test_db_path)
    for skill_id in SKILL_IDS:
        conn.execute(
            "INSERT INTO mastery (student_id, skill_id, score, last_practiced_at) VALUES (?, ?, ?, ?)",
            ("student-ananya", skill_id, 80.0, t0),
        )
    conn.close()

    # Track calls to decay_mastery to ensure every skill receives the exact same `now`
    from mastery_service import scoring
    decay_nows = []
    orig_decay = scoring.decay_mastery

    def spy_decay(score, last_practiced_at, now):
        decay_nows.append(now)
        return orig_decay(score, last_practiced_at, now)

    monkeypatch.setattr("mastery_service.main.decay_mastery", spy_decay)

    # Track time.time() calls originating from main.py
    import inspect
    main_time_calls = []
    real_time = time.time

    def counting_time():
        t = 2000000.0
        frame = inspect.currentframe().f_back
        if frame and "mastery_service" in frame.f_code.co_filename and "main.py" in frame.f_code.co_filename:
            main_time_calls.append(t)
        return t

    monkeypatch.setattr("time.time", counting_time)

    client = TestClient(app)
    resp = client.get(
        "/students/student-ananya/mastery",
        headers={"Authorization": "Bearer token-student-ananya"},
    )
    assert resp.status_code == 200
    data = resp.json()

    # Captured time in main.py must be called exactly once
    assert len(main_time_calls) == 1

    # Every practiced skill must have been decayed using that exact same instant
    assert len(decay_nows) == len(SKILL_IDS)
    assert len(set(decay_nows)) == 1
    assert decay_nows[0] == main_time_calls[0]

    # Scores must all be decayed identically
    scores = [item["mastery"] for item in data["mastery"]]
    assert len(set(scores)) == 1


# -------------------------------------------------------------------------
# 12. Future last_practiced_at does not produce negative/invalid mastery
# -------------------------------------------------------------------------
def test_future_last_practiced_at_does_not_produce_invalid_mastery(test_db_path, monkeypatch):
    now = 1000000.0
    future_time = now + 50000.0

    conn = get_connection(test_db_path)
    conn.execute(
        "INSERT INTO mastery (student_id, skill_id, score, last_practiced_at) VALUES (?, ?, ?, ?)",
        ("student-ananya", SKILL_IDS[0], 75.0, future_time),
    )
    conn.close()

    monkeypatch.setattr("time.time", lambda: now)

    client = TestClient(app)
    resp = client.get(
        "/students/student-ananya/mastery",
        headers={"Authorization": "Bearer token-student-ananya"},
    )
    assert resp.status_code == 200
    data = resp.json()
    mastery_map = {item["skill_id"]: item["mastery"] for item in data["mastery"]}

    # Score should be clamped to valid [0, 100] and not decayed negatively or corrupted
    assert mastery_map[SKILL_IDS[0]] == 75.0
    assert 0.0 <= mastery_map[SKILL_IDS[0]] <= 100.0


# -------------------------------------------------------------------------
# 13. Skills are returned in SKILL_IDS order
# -------------------------------------------------------------------------
def test_skills_returned_in_deterministic_skill_ids_order(test_db_path):
    # Insert mastery rows in reverse order of SKILL_IDS
    conn = get_connection(test_db_path)
    now = time.time()
    for skill in reversed(SKILL_IDS):
        conn.execute(
            "INSERT INTO mastery (student_id, skill_id, score, last_practiced_at) VALUES (?, ?, ?, ?)",
            ("student-ananya", skill, 70.0, now),
        )
    conn.close()

    client = TestClient(app)
    resp = client.get(
        "/students/student-ananya/mastery",
        headers={"Authorization": "Bearer token-student-ananya"},
    )
    assert resp.status_code == 200
    data = resp.json()

    returned_skill_ids = [item["skill_id"] for item in data["mastery"]]
    assert returned_skill_ids == SKILL_IDS
