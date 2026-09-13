"""
Given smoke tests, so you know the skeleton runs before you start.

These are NOT the tests your submission will be judged by — write your own
covering the tricky bits (the crash-during-notification requirement, the
rate-limit edge case, the flaky AI dependency). Feel free to add
tests/conftest.py, more test files, fixtures, whatever you'd normally set up.
"""

from fastapi.testclient import TestClient

from mastery_service.main import app

client = TestClient(app)


def test_health():
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_unknown_token_rejected():
    # Replace/extend once your endpoints exist — this just proves the
    # scaffolding's auth resolver plugs into a real route.
    resp = client.get(
        "/students/student-ananya/mastery",
        headers={"Authorization": "Bearer not-a-real-token"},
    )
    assert resp.status_code in (401, 404)  # 404 until you build the route
