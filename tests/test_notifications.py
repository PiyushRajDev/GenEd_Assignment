"""Tests for GET /notifications/{student_id} endpoint."""

import os
import sqlite3
import pytest
from fastapi.testclient import TestClient

from mastery_service.db import get_connection, init_db
from mastery_service.main import app
from mastery_service.seed_data import SKILL_IDS, TOKENS


@pytest.fixture
def test_db_path(tmp_path, monkeypatch):
    """File-based SQLite database for notification tests."""
    db_file = str(tmp_path / "test_notifications.db")
    monkeypatch.setenv("MASTERY_DB_PATH", db_file)
    init_db(db_file)
    yield db_file
    if os.path.exists(db_file):
        try:
            os.remove(db_file)
        except OSError:
            pass


# -------------------------------------------------------------------------
# 1. Student can view their own notifications -> 200
# -------------------------------------------------------------------------
def test_student_can_view_own_notifications(test_db_path):
    conn = get_connection(test_db_path)
    conn.execute(
        "INSERT INTO notifications (student_id, skill_id, reached_at) VALUES (?, ?, ?)",
        ("student-ananya", SKILL_IDS[0], 1000.0),
    )
    conn.close()

    client = TestClient(app)
    resp = client.get(
        "/notifications/student-ananya",
        headers={"Authorization": "Bearer token-student-ananya"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["student_id"] == "student-ananya"
    assert len(data["milestones"]) == 1
    assert data["milestones"][0]["skill_id"] == SKILL_IDS[0]
    assert data["milestones"][0]["reached_at"] == "1000.0"


# -------------------------------------------------------------------------
# 2. Teacher can view notifications for a student on their roster -> 200
# -------------------------------------------------------------------------
def test_teacher_can_view_rostered_student_notifications(test_db_path):
    conn = get_connection(test_db_path)
    conn.execute(
        "INSERT INTO notifications (student_id, skill_id, reached_at) VALUES (?, ?, ?)",
        ("student-ananya", SKILL_IDS[0], 1000.0),
    )
    conn.execute(
        "INSERT INTO notifications (student_id, skill_id, reached_at) VALUES (?, ?, ?)",
        ("student-rohan", SKILL_IDS[1], 2000.0),
    )
    conn.close()

    client = TestClient(app)
    # Teacher Kavita has student-ananya and student-rohan on her roster
    resp_ananya = client.get(
        "/notifications/student-ananya",
        headers={"Authorization": "Bearer token-teacher-kavita"},
    )
    assert resp_ananya.status_code == 200
    assert resp_ananya.json()["student_id"] == "student-ananya"
    assert len(resp_ananya.json()["milestones"]) == 1

    resp_rohan = client.get(
        "/notifications/student-rohan",
        headers={"Authorization": "Bearer token-teacher-kavita"},
    )
    assert resp_rohan.status_code == 200
    assert resp_rohan.json()["student_id"] == "student-rohan"
    assert len(resp_rohan.json()["milestones"]) == 1


# -------------------------------------------------------------------------
# 3. Student cannot view another student's notifications -> 404
# -------------------------------------------------------------------------
def test_student_cannot_view_other_student_notifications(test_db_path):
    conn = get_connection(test_db_path)
    conn.execute(
        "INSERT INTO notifications (student_id, skill_id, reached_at) VALUES (?, ?, ?)",
        ("student-ananya", SKILL_IDS[0], 1000.0),
    )
    conn.close()

    client = TestClient(app)
    # Rohan trying to view Ananya's notifications
    resp = client.get(
        "/notifications/student-ananya",
        headers={"Authorization": "Bearer token-student-rohan"},
    )
    assert resp.status_code == 404
    assert resp.json()["detail"] == "Not found"


# -------------------------------------------------------------------------
# 4. Teacher cannot view an unrostered student's notifications -> 404
# -------------------------------------------------------------------------
def test_teacher_cannot_view_unrostered_student_notifications(test_db_path):
    conn = get_connection(test_db_path)
    conn.execute(
        "INSERT INTO notifications (student_id, skill_id, reached_at) VALUES (?, ?, ?)",
        ("student-mei", SKILL_IDS[0], 1000.0),
    )
    conn.close()

    client = TestClient(app)
    # Mei is not on Kavita's roster
    resp = client.get(
        "/notifications/student-mei",
        headers={"Authorization": "Bearer token-teacher-kavita"},
    )
    assert resp.status_code == 404
    assert resp.json()["detail"] == "Not found"


# -------------------------------------------------------------------------
# 5. Missing token -> 401
# -------------------------------------------------------------------------
def test_missing_token_returns_401(test_db_path):
    client = TestClient(app)

    # Completely omitted Authorization header
    resp_missing = client.get("/notifications/student-ananya")
    assert resp_missing.status_code == 401
    assert resp_missing.json()["detail"] == "Invalid or missing token"

    # Empty token string
    resp_empty = client.get(
        "/notifications/student-ananya",
        headers={"Authorization": "Bearer "},
    )
    assert resp_empty.status_code == 401
    assert resp_empty.json()["detail"] == "Invalid or missing token"


# -------------------------------------------------------------------------
# 6. Unknown token -> 401
# -------------------------------------------------------------------------
def test_unknown_token_returns_401(test_db_path):
    client = TestClient(app)
    resp_unknown = client.get(
        "/notifications/student-ananya",
        headers={"Authorization": "Bearer not-a-valid-token-12345"},
    )
    assert resp_unknown.status_code == 401
    assert resp_unknown.json()["detail"] == "Invalid or missing token"


# -------------------------------------------------------------------------
# 7. Existing notifications are returned correctly
# -------------------------------------------------------------------------
def test_existing_notifications_returned_correctly(test_db_path):
    conn = get_connection(test_db_path)
    conn.execute(
        "INSERT INTO notifications (student_id, skill_id, reached_at) VALUES (?, ?, ?)",
        ("student-ananya", SKILL_IDS[0], 1710000000.5),
    )
    conn.execute(
        "INSERT INTO notifications (student_id, skill_id, reached_at) VALUES (?, ?, ?)",
        ("student-ananya", SKILL_IDS[1], 1710000100.0),
    )
    conn.close()

    client = TestClient(app)
    resp = client.get(
        "/notifications/student-ananya",
        headers={"Authorization": "Bearer token-student-ananya"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["student_id"] == "student-ananya"
    assert len(data["milestones"]) == 2
    assert data["milestones"][0] == {
        "skill_id": SKILL_IDS[0],
        "reached_at": "1710000000.5",
    }
    assert data["milestones"][1] == {
        "skill_id": SKILL_IDS[1],
        "reached_at": "1710000100.0",
    }


# -------------------------------------------------------------------------
# 8. Multiple notifications are returned in chronological order
# -------------------------------------------------------------------------
def test_multiple_notifications_returned_in_chronological_order(test_db_path):
    conn = get_connection(test_db_path)
    # Insert in reverse / mixed chronological order
    conn.execute(
        "INSERT INTO notifications (student_id, skill_id, reached_at) VALUES (?, ?, ?)",
        ("student-ananya", SKILL_IDS[2], 3000.0),
    )
    conn.execute(
        "INSERT INTO notifications (student_id, skill_id, reached_at) VALUES (?, ?, ?)",
        ("student-ananya", SKILL_IDS[0], 1000.0),
    )
    conn.execute(
        "INSERT INTO notifications (student_id, skill_id, reached_at) VALUES (?, ?, ?)",
        ("student-ananya", SKILL_IDS[1], 2000.0),
    )
    conn.close()

    client = TestClient(app)
    resp = client.get(
        "/notifications/student-ananya",
        headers={"Authorization": "Bearer token-student-ananya"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["milestones"]) == 3

    ordered_skills = [item["skill_id"] for item in data["milestones"]]
    ordered_timestamps = [float(item["reached_at"]) for item in data["milestones"]]

    assert ordered_skills == [SKILL_IDS[0], SKILL_IDS[1], SKILL_IDS[2]]
    assert ordered_timestamps == [1000.0, 2000.0, 3000.0]


# -------------------------------------------------------------------------
# 9. Same reached_at timestamps use id as deterministic tie-breaker
# -------------------------------------------------------------------------
def test_same_reached_at_timestamps_use_id_tie_breaker(test_db_path):
    conn = get_connection(test_db_path)
    # Insert 3 notifications with the exact same reached_at timestamp
    same_time = 1500.0
    conn.execute(
        "INSERT INTO notifications (id, student_id, skill_id, reached_at) VALUES (?, ?, ?, ?)",
        (10, "student-ananya", SKILL_IDS[2], same_time),
    )
    conn.execute(
        "INSERT INTO notifications (id, student_id, skill_id, reached_at) VALUES (?, ?, ?, ?)",
        (5, "student-ananya", SKILL_IDS[0], same_time),
    )
    conn.execute(
        "INSERT INTO notifications (id, student_id, skill_id, reached_at) VALUES (?, ?, ?, ?)",
        (8, "student-ananya", SKILL_IDS[1], same_time),
    )
    conn.close()

    client = TestClient(app)
    resp = client.get(
        "/notifications/student-ananya",
        headers={"Authorization": "Bearer token-student-ananya"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["milestones"]) == 3

    # Tie breaker id ASC: id 5 (SKILL_IDS[0]), id 8 (SKILL_IDS[1]), id 10 (SKILL_IDS[2])
    ordered_skills = [item["skill_id"] for item in data["milestones"]]
    assert ordered_skills == [SKILL_IDS[0], SKILL_IDS[1], SKILL_IDS[2]]


# -------------------------------------------------------------------------
# 10. Student with no notifications receives an empty response
# -------------------------------------------------------------------------
def test_student_with_no_notifications_returns_empty_response(test_db_path):
    client = TestClient(app)
    resp = client.get(
        "/notifications/student-ananya",
        headers={"Authorization": "Bearer token-student-ananya"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data == {
        "student_id": "student-ananya",
        "milestones": [],
    }


# -------------------------------------------------------------------------
# 11. GET does not modify notifications table
# -------------------------------------------------------------------------
def test_get_notifications_does_not_modify_notifications_table(test_db_path):
    t0 = 1000000.0
    conn = get_connection(test_db_path)
    conn.execute(
        "INSERT INTO notifications (student_id, skill_id, reached_at) VALUES (?, ?, ?)",
        ("student-ananya", SKILL_IDS[0], t0),
    )
    notifs_before = conn.execute("SELECT * FROM notifications ORDER BY id").fetchall()
    conn.close()

    client = TestClient(app)
    resp = client.get(
        "/notifications/student-ananya",
        headers={"Authorization": "Bearer token-student-ananya"},
    )
    assert resp.status_code == 200

    conn_after = get_connection(test_db_path)
    notifs_after = conn_after.execute("SELECT * FROM notifications ORDER BY id").fetchall()
    conn_after.close()

    assert len(notifs_after) == len(notifs_before)
    for row_b, row_a in zip(notifs_before, notifs_after):
        assert dict(row_b) == dict(row_a)


# -------------------------------------------------------------------------
# 12. GET does not modify mastery table
# -------------------------------------------------------------------------
def test_get_notifications_does_not_modify_mastery_table(test_db_path):
    t0 = 1000000.0
    conn = get_connection(test_db_path)
    conn.execute(
        "INSERT INTO mastery (student_id, skill_id, score, last_practiced_at) VALUES (?, ?, ?, ?)",
        ("student-ananya", SKILL_IDS[0], 85.0, t0),
    )
    conn.execute(
        "INSERT INTO notifications (student_id, skill_id, reached_at) VALUES (?, ?, ?)",
        ("student-ananya", SKILL_IDS[0], t0),
    )
    mastery_before = conn.execute("SELECT * FROM mastery ORDER BY student_id, skill_id").fetchall()
    conn.close()

    client = TestClient(app)
    resp = client.get(
        "/notifications/student-ananya",
        headers={"Authorization": "Bearer token-student-ananya"},
    )
    assert resp.status_code == 200

    conn_after = get_connection(test_db_path)
    mastery_after = conn_after.execute("SELECT * FROM mastery ORDER BY student_id, skill_id").fetchall()
    conn_after.close()

    assert len(mastery_after) == len(mastery_before)
    for row_b, row_a in zip(mastery_before, mastery_after):
        assert dict(row_b) == dict(row_a)


# -------------------------------------------------------------------------
# 13. GET does not create attempts
# -------------------------------------------------------------------------
def test_get_notifications_does_not_create_attempts(test_db_path):
    t0 = 1000000.0
    conn = get_connection(test_db_path)
    conn.execute(
        "INSERT INTO attempts (student_id, skill_id, is_correct, created_at) VALUES (?, ?, ?, ?)",
        ("student-ananya", SKILL_IDS[0], 1, t0),
    )
    conn.execute(
        "INSERT INTO notifications (student_id, skill_id, reached_at) VALUES (?, ?, ?)",
        ("student-ananya", SKILL_IDS[0], t0),
    )
    attempts_before = conn.execute("SELECT * FROM attempts ORDER BY id").fetchall()
    conn.close()

    client = TestClient(app)
    resp = client.get(
        "/notifications/student-ananya",
        headers={"Authorization": "Bearer token-student-ananya"},
    )
    assert resp.status_code == 200

    conn_after = get_connection(test_db_path)
    attempts_after = conn_after.execute("SELECT * FROM attempts ORDER BY id").fetchall()
    conn_after.close()

    assert len(attempts_after) == len(attempts_before)
    for row_b, row_a in zip(attempts_before, attempts_after):
        assert dict(row_b) == dict(row_a)


# -------------------------------------------------------------------------
# 14. GET does not call AI
# -------------------------------------------------------------------------
def test_get_notifications_does_not_call_ai(test_db_path, monkeypatch):
    t0 = 1000000.0
    conn = get_connection(test_db_path)
    conn.execute(
        "INSERT INTO notifications (student_id, skill_id, reached_at) VALUES (?, ?, ?)",
        ("student-ananya", SKILL_IDS[0], t0),
    )
    conn.close()

    ai_called = False

    def fake_get_ai_feedback(*args, **kwargs):
        nonlocal ai_called
        ai_called = True
        return "AI feedback"

    monkeypatch.setattr("mastery_service.main.get_ai_feedback", fake_get_ai_feedback)

    client = TestClient(app)
    resp = client.get(
        "/notifications/student-ananya",
        headers={"Authorization": "Bearer token-student-ananya"},
    )
    assert resp.status_code == 200
    assert not ai_called
