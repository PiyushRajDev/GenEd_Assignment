"""Mastery scoring engine using EWMA with time-based half-life decay."""

ALPHA = 0.20
HALF_LIFE_DAYS = 30
BASELINE_SEED = 50.0

SECONDS_PER_DAY = 86400.0


def decay_mastery(score: float, last_practiced_at: float, now: float) -> float:
    """Decay mastery score toward 0 based on elapsed time."""
    if now < last_practiced_at:
        return max(0.0, min(100.0, float(score)))

    elapsed_seconds = now - last_practiced_at
    decay_factor = 0.5 ** (elapsed_seconds / (HALF_LIFE_DAYS * SECONDS_PER_DAY))
    decayed_score = score * decay_factor
    return max(0.0, min(100.0, float(decayed_score)))


def update_mastery(
    previous_score: float | None,
    last_practiced_at: float | None,
    is_correct: bool,
    now: float,
) -> float:
    """Calculate updated mastery score using cold-start seed or decay followed by EWMA."""
    if previous_score is not None and last_practiced_at is None:
        raise ValueError("last_practiced_at must be provided when previous_score exists")

    if previous_score is None:
        current_score = BASELINE_SEED
    else:
        current_score = decay_mastery(previous_score, last_practiced_at, now)

    observed = 100.0 if is_correct else 0.0
    new_score = current_score + ALPHA * (observed - current_score)
    return max(0.0, min(100.0, float(new_score)))
