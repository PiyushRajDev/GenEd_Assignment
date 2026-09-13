"""Authorization logic and access-control checks for the Mastery Service."""

from fastapi import HTTPException

from mastery_service.seed_data import TEACHER_ROSTERS


def can_submit_attempt(identity: dict, student_id: str) -> bool:
    return identity.get("role") == "STUDENT" and identity.get("user_id") == student_id


def can_view_student(identity: dict, student_id: str) -> bool:
    
    role = identity.get("role")
    user_id = identity.get("user_id")

    if role == "STUDENT" and user_id == student_id:
        return True

    if role == "TEACHER":
        roster = TEACHER_ROSTERS.get(user_id, [])
        return student_id in roster

    return False


def require_submit_access(identity: dict, student_id: str) -> None:
    
    if not can_submit_attempt(identity, student_id):
        raise HTTPException(status_code=404, detail="Not found")


def require_view_access(identity: dict, student_id: str) -> None:

    if not can_view_student(identity, student_id):
        raise HTTPException(status_code=404, detail="Not found")
