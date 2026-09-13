"""Pure unit tests for the mastery scoring engine."""

import pytest

from mastery_service.scoring import (
    ALPHA,
    BASELINE_SEED,
    HALF_LIFE_DAYS,
    decay_mastery,
    update_mastery,
)

SECONDS_PER_DAY = 86400.0


def test_constants():
    assert ALPHA == 0.20
    assert HALF_LIFE_DAYS == 30
    assert BASELINE_SEED == 50.0


def test_cold_start_correct():
    result = update_mastery(
        previous_score=None,
        last_practiced_at=None,
        is_correct=True,
        now=1000.0,
    )
    assert result == 60.0
    assert result != BASELINE_SEED


def test_cold_start_incorrect():
    result = update_mastery(
        previous_score=None,
        last_practiced_at=None,
        is_correct=False,
        now=1000.0,
    )
    assert result == 40.0
    assert result != BASELINE_SEED


def test_decay_exact_half_life():
    last_practiced = 1000.0
    now = last_practiced + (HALF_LIFE_DAYS * SECONDS_PER_DAY)
    decayed = decay_mastery(score=80.0, last_practiced_at=last_practiced, now=now)
    assert decayed == 40.0


def test_decay_zero_elapsed():
    last_practiced = 1000.0
    now = last_practiced
    decayed = decay_mastery(score=80.0, last_practiced_at=last_practiced, now=now)
    assert decayed == 80.0


def test_decay_ten_half_lives_approaches_zero_not_fifty():
    last_practiced = 1000.0
    now = last_practiced + (10 * HALF_LIFE_DAYS * SECONDS_PER_DAY)
    decayed = decay_mastery(score=80.0, last_practiced_at=last_practiced, now=now)

    expected = 80.0 * (0.5 ** 10)
    assert pytest.approx(decayed) == expected
    assert decayed < 0.1
    assert decayed > 0.0
    # Explicitly verify it does not decay toward 50.0
    assert decayed < 50.0


def test_negative_elapsed_time():
    last_practiced = 2000.0
    now = 1000.0  # now < last_practiced_at

    decayed = decay_mastery(score=80.0, last_practiced_at=last_practiced, now=now)
    assert decayed == 80.0

    # update_mastery also does not raise and treats elapsed time as zero
    updated = update_mastery(
        previous_score=60.0,
        last_practiced_at=last_practiced,
        is_correct=True,
        now=now,
    )
    assert updated == 68.0


def test_ewma_known_scores_zero_elapsed():
    last_practiced = 1000.0
    now = 1000.0

    correct_result = update_mastery(
        previous_score=60.0,
        last_practiced_at=last_practiced,
        is_correct=True,
        now=now,
    )
    assert correct_result == 68.0

    incorrect_result = update_mastery(
        previous_score=60.0,
        last_practiced_at=last_practiced,
        is_correct=False,
        now=now,
    )
    assert incorrect_result == 48.0


def test_score_bounds_at_boundaries():
    last_practiced = 1000.0
    now = 1000.0

    max_correct = update_mastery(
        previous_score=100.0,
        last_practiced_at=last_practiced,
        is_correct=True,
        now=now,
    )
    assert max_correct == 100.0

    min_incorrect = update_mastery(
        previous_score=0.0,
        last_practiced_at=last_practiced,
        is_correct=False,
        now=now,
    )
    assert min_incorrect == 0.0

    assert 0.0 <= decay_mastery(0.0, last_practiced, now) <= 100.0
    assert 0.0 <= decay_mastery(100.0, last_practiced, now) <= 100.0


def test_determinism():
    res1 = update_mastery(
        previous_score=75.0,
        last_practiced_at=1000.0,
        is_correct=True,
        now=2000.0,
    )
    res2 = update_mastery(
        previous_score=75.0,
        last_practiced_at=1000.0,
        is_correct=True,
        now=2000.0,
    )
    assert res1 == res2


def test_invalid_state_raises_value_error():
    with pytest.raises(ValueError):
        update_mastery(
            previous_score=60.0,
            last_practiced_at=None,
            is_correct=True,
            now=1000.0,
        )

    with pytest.raises(ValueError):
        update_mastery(
            previous_score=0.0,
            last_practiced_at=None,
            is_correct=False,
            now=1000.0,
        )
