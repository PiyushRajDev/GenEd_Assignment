"""Tests for authorization policies and access control wrappers."""

import pytest
from fastapi import HTTPException

from mastery_service.auth import (
    can_submit_attempt,
    can_view_student,
    require_submit_access,
    require_view_access,
)
from mastery_service.main import get_current_identity
from mastery_service.seed_data import TOKENS


@pytest.fixture
def ananya_identity() -> dict:
    return TOKENS["token-student-ananya"]


@pytest.fixture
def rohan_identity() -> dict:
    return TOKENS["token-student-rohan"]


@pytest.fixture
def kavita_identity() -> dict:
    return TOKENS["token-teacher-kavita"]


# 1. student-ananya submitting her own attempt -> allowed
def test_student_ananya_can_submit_own_attempt(ananya_identity):
    assert can_submit_attempt(ananya_identity, "student-ananya") is True
    # Verify require_submit_access does not raise
    require_submit_access(ananya_identity, "student-ananya")


# 2. student-ananya viewing her own data -> allowed
def test_student_ananya_can_view_own_data(ananya_identity):
    assert can_view_student(ananya_identity, "student-ananya") is True
    # Verify require_view_access does not raise
    require_view_access(ananya_identity, "student-ananya")


# 3. student-ananya trying to submit an attempt as student-rohan -> denied with 404
def test_student_ananya_cannot_submit_attempt_for_rohan(ananya_identity):
    assert can_submit_attempt(ananya_identity, "student-rohan") is False
    with pytest.raises(HTTPException) as exc_info:
        require_submit_access(ananya_identity, "student-rohan")
    assert exc_info.value.status_code == 404
    assert exc_info.value.detail == "Not found"


# 4. student-ananya trying to view student-rohan's data -> denied with 404
def test_student_ananya_cannot_view_rohan_data(ananya_identity):
    assert can_view_student(ananya_identity, "student-rohan") is False
    with pytest.raises(HTTPException) as exc_info:
        require_view_access(ananya_identity, "student-rohan")
    assert exc_info.value.status_code == 404
    assert exc_info.value.detail == "Not found"


# 5. teacher-kavita viewing student-ananya -> allowed because student-ananya is on her roster
def test_teacher_kavita_can_view_rostered_student_ananya(kavita_identity):
    assert can_view_student(kavita_identity, "student-ananya") is True
    require_view_access(kavita_identity, "student-ananya")


# 6. teacher-kavita viewing student-mei -> denied with 404 because student-mei is not on her roster
def test_teacher_kavita_cannot_view_unrostered_student_mei(kavita_identity):
    assert can_view_student(kavita_identity, "student-mei") is False
    with pytest.raises(HTTPException) as exc_info:
        require_view_access(kavita_identity, "student-mei")
    assert exc_info.value.status_code == 404
    assert exc_info.value.detail == "Not found"


# 7. teacher-kavita attempting to submit an attempt as student-ananya -> denied with 404
def test_teacher_kavita_cannot_submit_attempt_for_ananya(kavita_identity):
    assert can_submit_attempt(kavita_identity, "student-ananya") is False
    with pytest.raises(HTTPException) as exc_info:
        require_submit_access(kavita_identity, "student-ananya")
    assert exc_info.value.status_code == 404
    assert exc_info.value.detail == "Not found"


# 8. teacher-kavita attempting to submit an attempt as student-rohan -> denied with 404
def test_teacher_kavita_cannot_submit_attempt_for_rohan(kavita_identity):
    assert can_submit_attempt(kavita_identity, "student-rohan") is False
    with pytest.raises(HTTPException) as exc_info:
        require_submit_access(kavita_identity, "student-rohan")
    assert exc_info.value.status_code == 404
    assert exc_info.value.detail == "Not found"


# 9. Unknown/garbage token -> existing get_current_identity() behavior remains 401
def test_unknown_token_raises_401():
    with pytest.raises(HTTPException) as exc_info:
        get_current_identity("Bearer not-a-real-token")
    assert exc_info.value.status_code == 401
    assert exc_info.value.detail == "Invalid or missing token"


def test_missing_token_raises_401():
    with pytest.raises(HTTPException) as exc_info:
        get_current_identity("")
    assert exc_info.value.status_code == 401
    assert exc_info.value.detail == "Invalid or missing token"


# Additional edge cases: unrostered teacher and non-403 guarantees
def test_teacher_without_roster_denied_view_with_404():
    unknown_teacher = {"role": "TEACHER", "user_id": "teacher-unknown"}
    assert can_view_student(unknown_teacher, "student-ananya") is False
    with pytest.raises(HTTPException) as exc_info:
        require_view_access(unknown_teacher, "student-ananya")
    assert exc_info.value.status_code == 404
    assert exc_info.value.detail == "Not found"
