import asyncio
import sqlite3
import time
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse

from mastery_service.ai_feedback import get_ai_feedback
from mastery_service.attempts import RateLimitExceeded, record_attempt
from mastery_service.auth import require_submit_access, require_view_access
from mastery_service.db import get_db, init_db
from mastery_service.schemas import (
    AttemptRequest,
    AttemptResponse,
    MasteryItem,
    MasteryResponse,
    NotificationItem,
    NotificationResponse,
)
from mastery_service.scoring import BASELINE_SEED, decay_mastery
from mastery_service.seed_data import SKILL_IDS, TOKENS

AI_TIMEOUT_SECONDS = 6


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
async def submit_attempt(
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

    # AI feedback is best-effort enrichment, called only after the domain
    # transaction has committed so that AI latency/failure never blocks or
    # rolls back the authoritative state change.
    feedback = None
    feedback_status = "unavailable"
    try:
        async with asyncio.timeout(AI_TIMEOUT_SECONDS):
            feedback = await asyncio.to_thread(
                get_ai_feedback, attempt.skill_id, attempt.is_correct,
            )
        feedback_status = "ok"
    except Exception:
        pass

    return AttemptResponse(
        skill_id=result.skill_id,
        mastery=result.mastery,
        milestone_reached=result.milestone_reached,
        feedback=feedback,
        feedback_status=feedback_status,
    )


@app.get("/students/{student_id}/mastery", response_model=MasteryResponse)
def get_student_mastery(
    student_id: str,
    identity: dict = Depends(get_current_identity),
    conn: sqlite3.Connection = Depends(get_db),
) -> MasteryResponse:
    require_view_access(identity, student_id)

    now = time.time()

    cursor = conn.execute(
        "SELECT skill_id, score, last_practiced_at FROM mastery WHERE student_id = ?",
        (student_id,),
    )
    stored_mastery = {
        row["skill_id"]: (float(row["score"]), float(row["last_practiced_at"]))
        for row in cursor.fetchall()
    }

    mastery_items = []
    for skill_id in SKILL_IDS:
        if skill_id in stored_mastery:
            score, last_practiced_at = stored_mastery[skill_id]
            current_score = decay_mastery(score, last_practiced_at, now)
        else:
            current_score = BASELINE_SEED

        mastery_items.append(MasteryItem(skill_id=skill_id, mastery=current_score))

    return MasteryResponse(student_id=student_id, mastery=mastery_items)


@app.get("/notifications/{student_id}", response_model=NotificationResponse)
def get_student_notifications(
    student_id: str,
    identity: dict = Depends(get_current_identity),
    conn: sqlite3.Connection = Depends(get_db),
) -> NotificationResponse:
    require_view_access(identity, student_id)

    cursor = conn.execute(
        """
        SELECT skill_id, reached_at
        FROM notifications
        WHERE student_id = ?
        ORDER BY reached_at ASC, id ASC
        """,
        (student_id,),
    )
    items = [
        NotificationItem(
            skill_id=row["skill_id"],
            reached_at=str(row["reached_at"]),
        )
        for row in cursor.fetchall()
    ]

    return NotificationResponse(student_id=student_id, milestones=items)

