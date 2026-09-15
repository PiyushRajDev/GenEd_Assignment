import asyncio
import concurrent.futures
import sqlite3
import threading
import time
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse

from mastery_service.ai_feedback import get_ai_feedback
from mastery_service.attempts import (
    AttemptResult,
    IdempotencyConflict,
    RateLimitExceeded,
    record_attempt,
)
from mastery_service.auth import require_submit_access, require_view_access
from mastery_service.db import get_connection, get_db, init_db
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

AI_MAX_WORKERS = 16
AI_TIMEOUT_SECONDS = 6

ai_executor = concurrent.futures.ThreadPoolExecutor(
    max_workers=AI_MAX_WORKERS,
    thread_name_prefix="ai-feedback",
)
ai_semaphore = threading.Semaphore(AI_MAX_WORKERS)


def _record_attempt_in_worker(
    student_id: str,
    skill_id: str,
    is_correct: bool,
    idempotency_key: str | None = None,
) -> AttemptResult:
    """Run record_attempt in a dedicated thread with its own connection lifecycle."""
    conn = get_connection()
    try:
        return record_attempt(
            conn=conn,
            student_id=student_id,
            skill_id=skill_id,
            is_correct=is_correct,
            idempotency_key=idempotency_key,
        )
    finally:
        conn.close()


def _run_ai_feedback_worker(skill_id: str, is_correct: bool) -> str:
    """Invoke get_ai_feedback on worker thread and release capacity slot upon completion."""
    try:
        return get_ai_feedback(skill_id, is_correct)
    finally:
        ai_semaphore.release()


async def fetch_ai_feedback(skill_id: str, is_correct: bool) -> tuple[str | None, str]:
    """Fetch AI feedback using bounded worker pool and strict timeout.

    Returns (feedback, feedback_status).
    If worker slots are exhausted, immediately returns (None, 'unavailable') without queuing.
    If timeout expires or provider fails, returns (None, 'unavailable').
    The worker capacity remains occupied until the underlying thread finishes.
    """
    if not ai_semaphore.acquire(blocking=False):
        return None, "unavailable"

    loop = asyncio.get_running_loop()
    try:
        try:
            future = loop.run_in_executor(
                ai_executor,
                _run_ai_feedback_worker,
                skill_id,
                is_correct,
            )
        except Exception:
            ai_semaphore.release()
            return None, "unavailable"

        async with asyncio.timeout(AI_TIMEOUT_SECONDS):
            feedback = await future
            return feedback, "ok"
    except Exception:
        return None, "unavailable"


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield
    ai_executor.shutdown(wait=False)


app = FastAPI(title="GenEd Mastery Service — Take-Home", lifespan=lifespan)


@app.exception_handler(RateLimitExceeded)
def handle_rate_limit_exceeded(request: Request, exc: RateLimitExceeded) -> JSONResponse:
    return JSONResponse(
        status_code=429,
        content={"detail": exc.detail, "retry_after_seconds": exc.retry_after_seconds},
        headers={"Retry-After": str(exc.retry_after_seconds)},
    )


@app.exception_handler(IdempotencyConflict)
def handle_idempotency_conflict(request: Request, exc: IdempotencyConflict) -> JSONResponse:
    return JSONResponse(
        status_code=409,
        content={"detail": exc.detail},
    )


def get_current_identity(authorization: str = Header(default="")) -> dict:
    
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
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    identity: dict = Depends(get_current_identity),
) -> AttemptResponse:
    require_submit_access(identity, student_id)

    result = await asyncio.to_thread(
        _record_attempt_in_worker,
        student_id=student_id,
        skill_id=attempt.skill_id,
        is_correct=attempt.is_correct,
        idempotency_key=idempotency_key,
    )

    feedback = None
    feedback_status = "unavailable"
    if not result.is_replayed:
        feedback, feedback_status = await fetch_ai_feedback(
            attempt.skill_id, attempt.is_correct
        )

    return AttemptResponse(
        skill_id=result.skill_id,
        mastery=round(result.mastery, 2),
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

        mastery_items.append(MasteryItem(skill_id=skill_id, mastery=round(current_score, 2)))

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

