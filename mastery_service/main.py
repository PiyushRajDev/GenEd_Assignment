"""
STARTER FILE — build the actual service here (or restructure into more
files/modules if you prefer; this single-file skeleton is just a starting
point, not a requirement).

See PROBLEM.md for the full spec. Summary of what needs to exist by the end:

  POST /students/{student_id}/attempts
      Body: {"skill_id": str, "is_correct": bool}
      - Only the student themselves may submit their own attempts.
      - Updates that student's mastery score (0-100) for that skill using a
        scoring approach YOU design and justify in WRITEUP.md.
      - Calls get_ai_feedback() (see ai_feedback.py) to get a feedback
        string for the response. That call is slow and sometimes fails —
        your endpoint must still behave well when it does.
      - Enforces: max 30 attempts per student per rolling 24h. A request
        that fails validation (bad skill_id, malformed body, etc.) must NOT
        count against that limit.
      - If this attempt takes the student's mastery for that skill above 80
        for the first time, a milestone notification must be durably
        recorded — including surviving a crash between "mastery updated"
        and "notification recorded."

  GET /students/{student_id}/mastery
      - A student may view their own mastery.
      - A teacher may view mastery for any student on their own roster
        (see seed_data.TEACHER_ROSTERS), and no one else's.
      - Returns current mastery per skill for that student.

  GET /notifications/{student_id}
      - Same access rule as above. Returns the milestone notifications
        recorded for that student.

Everything below this docstring is scaffolding, not a solution — feel free
to delete, restructure, or heavily rewrite it.
"""

import sqlite3
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse

from mastery_service.attempts import RateLimitExceeded, record_attempt
from mastery_service.auth import require_submit_access
from mastery_service.db import get_db, init_db
from mastery_service.schemas import AttemptRequest, AttemptResponse
from mastery_service.seed_data import TOKENS


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(title="GenEd Mastery Service — Take-Home", lifespan=lifespan)


@app.exception_handler(RateLimitExceeded)
def handle_rate_limit_exceeded(request: Request, exc: RateLimitExceeded) -> JSONResponse:
    return JSONResponse(
        status_code=429,
        content={"detail": exc.detail, "retry_after_seconds": exc.retry_after_seconds},
        headers={"Retry-After": str(exc.retry_after_seconds)},
    )


def get_current_identity(authorization: str = Header(default="")) -> dict:
    """Resolve the Authorization header into {"role", "user_id"}.

    This is deliberately trivial — see seed_data.py for how the token map
    works. Raise HTTPException(401) for a missing/unknown token.
    """
    token = authorization.removeprefix("Bearer ").strip()
    identity = TOKENS.get(token)
    if identity is None:
        raise HTTPException(status_code=401, detail="Invalid or missing token")
    return identity


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.post("/students/{student_id}/attempts", response_model=AttemptResponse)
def submit_attempt(
    student_id: str,
    attempt: AttemptRequest,
    identity: dict = Depends(get_current_identity),
    conn: sqlite3.Connection = Depends(get_db),
) -> AttemptResponse:
    require_submit_access(identity, student_id)

    result = record_attempt(
        conn=conn,
        student_id=student_id,
        skill_id=attempt.skill_id,
        is_correct=attempt.is_correct,
    )

    return AttemptResponse(
        skill_id=result.skill_id,
        mastery=result.mastery,
        milestone_reached=result.milestone_reached,
        feedback=None,
        feedback_status="unavailable",
    )


# TODO: GET /students/{student_id}/mastery
# TODO: GET /notifications/{student_id}
