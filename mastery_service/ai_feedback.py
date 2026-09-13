"""
GIVEN — do not rewrite this file.

This simulates GenEd's real AI feedback provider: it is slow, and it fails
sometimes. That's the point. Your job is to build an /attempts endpoint that
behaves well when this function is slow or throws, not to make this function
better.

Real behaviour you should assume from a real AI provider:
  - Latency varies a lot, call to call (0.5s - 5s here; in prod it's worse).
  - It fails outright sometimes (network blip, provider overload, timeout on
    THEIR end) — about 15% of calls here.
  - A failure does not tell you whether the provider "did the work" or not —
    treat it as "unknown", not as "definitely nothing happened."

Do not remove the sleep or the random failure to make your own testing
easier — instead, make your code robust to them (and feel free to write
tests that monkeypatch or wrap this function so your suite runs fast).
"""

import random
import time


class AIFeedbackError(Exception):
    """Raised when the (simulated) AI provider fails to produce feedback."""


def get_ai_feedback(skill_id: str, is_correct: bool) -> str:
    """Blocking call. Sleeps 0.5-5s, then either returns a feedback string
    or raises AIFeedbackError (~15% of calls).

    In a real system this would be a network call to an LLM provider.
    """
    time.sleep(random.uniform(0.5, 5.0))

    if random.random() < 0.15:
        raise AIFeedbackError(f"AI feedback provider timed out for skill={skill_id}")

    if is_correct:
        return f"Nice work on {skill_id} — you're building real fluency here."
    return f"Not quite on {skill_id} — review the last step and try a similar problem."
