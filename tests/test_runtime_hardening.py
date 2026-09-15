"""Focused tests for runtime hardening fixes:
1. DB operations in POST /attempts offloaded to a worker thread with dedicated connection lifecycle.
2. Bounded AI concurrency with immediate degradation on saturation and slot retention on timeout.
3. Mastery values returned by the API rounded to 2 decimal places while preserving stored precision.
"""

import asyncio
import os
import sqlite3
import threading
import time
import httpx
import pytest
from fastapi.testclient import TestClient

from mastery_service.db import get_connection, init_db
from mastery_service.main import (
    AI_MAX_WORKERS,
    app,
    ai_semaphore,
)
from mastery_service.seed_data import SKILL_IDS

SKILL_A = SKILL_IDS[0]


@pytest.fixture
def test_db_path(tmp_path, monkeypatch):
    """File-based SQLite database for runtime tests."""
    db_file = str(tmp_path / "test_hardening.db")
    monkeypatch.setenv("MASTERY_DB_PATH", db_file)
    init_db(db_file)
    yield db_file
    if os.path.exists(db_file):
        try:
            os.remove(db_file)
        except OSError:
            pass


# =========================================================================
# 1. DB Operation Offloaded to Worker Thread
# =========================================================================

def test_db_operation_runs_in_separate_worker_thread(test_db_path, monkeypatch):
    """Verify record_attempt executes on a worker thread, distinct from the caller/event loop."""
    recorded_threads = []
    from mastery_service import main as main_mod
    orig_record_attempt = main_mod.record_attempt

    def spy_record_attempt(conn, student_id, skill_id, is_correct, **kwargs):
        recorded_threads.append(threading.current_thread().ident)
        # Verify SQLite connection is valid and created on this thread
        cur = conn.execute("SELECT 1")
        assert cur.fetchone()[0] == 1
        return orig_record_attempt(conn, student_id, skill_id, is_correct, **kwargs)

    monkeypatch.setattr(main_mod, "record_attempt", spy_record_attempt)
    monkeypatch.setattr(main_mod, "get_ai_feedback", lambda s, c: "ok")

    main_thread_ident = threading.current_thread().ident

    client = TestClient(app)
    resp = client.post(
        "/students/student-ananya/attempts",
        headers={"Authorization": "Bearer token-student-ananya"},
        json={"skill_id": SKILL_A, "is_correct": True},
    )

    assert resp.status_code == 200
    assert len(recorded_threads) == 1
    # The DB operation thread must be distinct from the test/main thread
    assert recorded_threads[0] != main_thread_ident


@pytest.mark.anyio
async def test_db_operation_does_not_block_event_loop_during_slow_transaction(test_db_path, monkeypatch):
    """Verify that a slow/blocking DB transaction in POST /attempts does not freeze the asyncio event loop."""
    from mastery_service import main as main_mod
    orig_record = main_mod.record_attempt

    db_entered = threading.Event()
    release_db = threading.Event()

    def slow_record_attempt(conn, student_id, skill_id, is_correct, **kwargs):
        db_entered.set()
        # Simulate blocking wait (e.g. busy_timeout contention)
        release_db.wait(timeout=2.0)
        return orig_record(conn, student_id, skill_id, is_correct, **kwargs)

    monkeypatch.setattr(main_mod, "record_attempt", slow_record_attempt)
    monkeypatch.setattr(main_mod, "get_ai_feedback", lambda s, c: "ok")

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        # Launch slow POST attempt in background
        post_task = asyncio.create_task(
            client.post(
                "/students/student-ananya/attempts",
                headers={"Authorization": "Bearer token-student-ananya"},
                json={"skill_id": SKILL_A, "is_correct": True},
            )
        )

        # Wait until the worker thread has entered the slow DB operation
        while not db_entered.is_set():
            await asyncio.sleep(0.01)

        # While DB operation is still blocked, the event loop should be fully responsive
        health_resp = await client.get("/health")
        assert health_resp.status_code == 200
        assert health_resp.json() == {"status": "ok"}

        # Now release the slow DB operation
        release_db.set()
        post_resp = await post_task
        assert post_resp.status_code == 200
        assert post_resp.json()["mastery"] == 60.0


# =========================================================================
# 2. Bounded AI Concurrency and Immediate Degradation
# =========================================================================

def test_ai_concurrency_bounded_and_immediate_degradation(test_db_path, monkeypatch):
    """When all 16 AI worker slots are saturated, new attempts must immediately return
    feedback_status='unavailable' without waiting or queueing unbounded work.
    """
    from mastery_service import main as main_mod

    workers_blocked = threading.Event()
    release_workers = threading.Event()
    active_count_lock = threading.Lock()
    active_count = {"count": 0}

    def slow_ai_provider(skill_id, is_correct):
        with active_count_lock:
            active_count["count"] += 1
            if active_count["count"] == AI_MAX_WORKERS:
                workers_blocked.set()
        release_workers.wait(timeout=5.0)
        return "feedback"

    monkeypatch.setattr(main_mod, "get_ai_feedback", slow_ai_provider)

    threads = []
    responses = []

    def submit_attempt_worker(student_id):
        c = TestClient(app)
        r = c.post(
            f"/students/{student_id}/attempts",
            headers={"Authorization": f"Bearer token-{student_id}"},
            json={"skill_id": SKILL_A, "is_correct": True},
        )
        responses.append(r)

    # Launch AI_MAX_WORKERS (16) requests concurrently to fill the pool
    # Use different students (or valid token)
    for i in range(AI_MAX_WORKERS):
        t = threading.Thread(target=submit_attempt_worker, args=("student-ananya",))
        threads.append(t)
        t.start()

    # Wait until all 16 slots are occupied in the AI provider
    assert workers_blocked.wait(timeout=3.0), "Timed out waiting for 16 AI workers to occupy slots"

    # Now verify that request #17 immediately returns feedback_status='unavailable'
    client = TestClient(app)
    t_start = time.time()
    overflow_resp = client.post(
        "/students/student-rohan/attempts",
        headers={"Authorization": "Bearer token-student-rohan"},
        json={"skill_id": SKILL_A, "is_correct": True},
    )
    t_elapsed = time.time() - t_start

    # The 17th request must complete immediately (e.g. < 0.2s, not waiting for AI workers)
    assert t_elapsed < 0.5
    assert overflow_resp.status_code == 200
    overflow_data = overflow_resp.json()
    assert overflow_data["feedback"] is None
    assert overflow_data["feedback_status"] == "unavailable"
    # Authoritative state change is fully committed
    assert overflow_data["mastery"] == 60.0

    # Unblock the workers and join
    release_workers.set()
    for t in threads:
        t.join(timeout=3.0)

    # All initial 16 requests should have completed successfully with ok feedback
    assert len(responses) == AI_MAX_WORKERS
    for r in responses:
        assert r.status_code == 200
        assert r.json()["feedback_status"] == "ok"


def test_ai_timed_out_thread_retains_slot_until_finished(test_db_path, monkeypatch):
    """When an AI call times out, the HTTP request returns unavailable immediately,
    but the worker slot remains occupied until the underlying thread finishes.
    """
    from mastery_service import main as main_mod

    thread_running = threading.Event()
    allow_finish = threading.Event()

    def stubborn_slow_ai(skill_id, is_correct):
        thread_running.set()
        allow_finish.wait(timeout=3.0)
        return "late feedback"

    monkeypatch.setattr(main_mod, "get_ai_feedback", stubborn_slow_ai)
    monkeypatch.setattr(main_mod, "AI_TIMEOUT_SECONDS", 0.05)

    client = TestClient(app)

    # Acquire initial available capacity
    slots_before = ai_semaphore._value

    resp = client.post(
        "/students/student-ananya/attempts",
        headers={"Authorization": "Bearer token-student-ananya"},
        json={"skill_id": SKILL_A, "is_correct": True},
    )

    assert resp.status_code == 200
    assert resp.json()["feedback_status"] == "unavailable"

    # Verify the thread is still running and holding its slot
    assert thread_running.is_set()
    # The semaphore value should be decremented by 1 while thread is still active
    assert ai_semaphore._value == slots_before - 1

    # Now let the thread finish
    allow_finish.set()
    # Wait briefly for thread to exit and release semaphore
    time.sleep(0.1)
    assert ai_semaphore._value == slots_before


# =========================================================================
# 3. Mastery Values Returned Rounded to 2 Decimal Places
# =========================================================================

def test_get_mastery_rounds_to_two_decimal_places(test_db_path, monkeypatch):
    """GET /mastery returns values rounded to 2 decimal places, while stored score is unrounded."""
    conn = get_connection(test_db_path)
    t0 = 1000000.0
    # Stored score is unrounded float
    unrounded_stored_score = 79.9987168
    conn.execute(
        "INSERT INTO mastery (student_id, skill_id, score, last_practiced_at) VALUES (?, ?, ?, ?)",
        ("student-ananya", SKILL_A, unrounded_stored_score, t0),
    )
    conn.close()

    # Time advancing by 60 seconds produces floating decay
    t_read = t0 + 60.0
    monkeypatch.setattr("time.time", lambda: t_read)

    client = TestClient(app)
    resp = client.get(
        "/students/student-ananya/mastery",
        headers={"Authorization": "Bearer token-student-ananya"},
    )
    assert resp.status_code == 200
    data = resp.json()

    item_a = next(it for it in data["mastery"] if it["skill_id"] == SKILL_A)
    # 79.9987168 decayed by 60s is ~79.997... rounded to 2 decimals is 80.0
    assert item_a["mastery"] == round(item_a["mastery"], 2)
    assert str(item_a["mastery"]).split(".")[1] in ("0", "00", str(item_a["mastery"]).split(".")[1][:2])

    # Check underlying DB score is not rounded
    conn = get_connection(test_db_path)
    db_score = conn.execute(
        "SELECT score FROM mastery WHERE student_id = ? AND skill_id = ?",
        ("student-ananya", SKILL_A),
    ).fetchone()[0]
    conn.close()
    assert db_score == unrounded_stored_score


def test_post_attempt_rounds_response_mastery(test_db_path, monkeypatch):
    """POST /attempts returns mastery rounded to 2 decimal places, leaving DB precision intact."""
    monkeypatch.setattr("mastery_service.main.get_ai_feedback", lambda s, c: "ok")

    # Seed score with a value that produces a fractional score with many decimal digits
    conn = get_connection(test_db_path)
    t0 = 1000000.0
    conn.execute(
        "INSERT INTO mastery (student_id, skill_id, score, last_practiced_at) VALUES (?, ?, ?, ?)",
        ("student-ananya", SKILL_A, 71.3333333333, t0),
    )
    conn.close()

    # Next attempt at t0 (no decay): 71.3333333333 + 0.2*(100 - 71.3333333333) = 77.06666666664
    monkeypatch.setattr("time.time", lambda: t0)

    client = TestClient(app)
    resp = client.post(
        "/students/student-ananya/attempts",
        headers={"Authorization": "Bearer token-student-ananya"},
        json={"skill_id": SKILL_A, "is_correct": True},
    )
    assert resp.status_code == 200
    data = resp.json()

    assert data["mastery"] == 77.07
    # Exactly 2 decimal places in returned response
    assert round(data["mastery"], 2) == data["mastery"]

    # Verify that SQLite persists the unrounded float
    conn = get_connection(test_db_path)
    db_score = conn.execute(
        "SELECT score FROM mastery WHERE student_id = ? AND skill_id = ?",
        ("student-ananya", SKILL_A),
    ).fetchone()[0]
    conn.close()
    assert abs(db_score - 77.06666666664) < 1e-6
