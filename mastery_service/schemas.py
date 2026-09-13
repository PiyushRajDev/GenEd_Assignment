"""Pydantic schemas for the Mastery Service."""

from typing import Literal
from pydantic import BaseModel

from mastery_service.seed_data import SKILL_IDS

# Literal type built from seed_data.SKILL_IDS
SkillId = Literal[tuple(SKILL_IDS)]


class AttemptRequest(BaseModel):
    skill_id: SkillId
    is_correct: bool


class AttemptResponse(BaseModel):
    skill_id: str
    mastery: float
    milestone_reached: bool
    feedback: str | None = None
    feedback_status: Literal["ok", "unavailable"]


class RateLimitError(BaseModel):
    detail: str
    retry_after_seconds: int


class MasteryItem(BaseModel):
    skill_id: str
    mastery: float
    last_practiced_at: str


class MasteryResponse(BaseModel):
    student_id: str
    mastery: list[MasteryItem]


class NotificationItem(BaseModel):
    skill_id: str
    reached_at: str


class NotificationResponse(BaseModel):
    student_id: str
    milestones: list[NotificationItem]
