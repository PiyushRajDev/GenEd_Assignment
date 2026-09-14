# Writeup

## 1. Mastery scoring

**Formula:** EWMA (Exponentially Weighted Moving Average) with time-based half-life decay.

Each attempt updates the score as:

```
new_score = current_score + α × (observed − current_score)
```

where `α = 0.20`, `observed = 100` for correct and `0` for incorrect, and `current_score` is the stored score after applying decay since the student last practiced.

Before the EWMA update, the stored score is decayed toward zero based on elapsed time:

```
decay_factor = 0.5 ^ (elapsed_seconds / (HALF_LIFE_DAYS × 86400))
decayed_score = stored_score × decay_factor
```

`HALF_LIFE_DAYS = 30`, so after 30 days of inactivity, a score of 80 becomes 40. For a student's very first attempt on a skill, there is no stored score, so we seed from `BASELINE_SEED = 50.0` (no decay applied on cold start since there's no prior timestamp).

**Why this approach:** EWMA is simple, deterministic, and gives recent attempts more weight than older ones without needing to store or replay the full attempt history. The mastery table only holds `(score, last_practiced_at)`. The half-life decay models the real phenomenon that skills fade without practice, and decaying toward 0 (not toward 50) means a student who stops practicing will eventually show near-zero mastery rather than settling at an ambiguous midpoint.

**What it gets wrong / what a better version would fix:**

- The α and half-life constants are hardcoded guesses. A production system would calibrate these from real student performance data, possibly per-skill or per-difficulty-tier.
- A single scalar score doesn't distinguish *consistency* from *recent lucky streak*. A student who gets 5 right in a row after 20 wrong looks the same as one who has been steadily improving. A better model might track a confidence interval or use a Bayesian approach (e.g. BKT or Elo-style rating) that separates "likely knows" from "uncertain".
- The decay model is purely time-based and doesn't account for spaced-repetition effects. Ideally, a skill practiced in well-spaced intervals should decay slower than one crammed in a single session.
- α = 0.20 means each attempt shifts the score by at most 20 points, so climbing from 50 to 80+ takes a minimum of ~3 consecutive correct answers. That might be too lenient or too strict depending on the skill.

## 2. The flaky AI dependency

The `/attempts` endpoint treats AI feedback as **best-effort enrichment** that is strictly decoupled from the core domain transaction. The sequence:

1. The domain transaction (`record_attempt`) runs first — attempt insertion, mastery update, and milestone notification all happen inside a single `BEGIN IMMEDIATE ... COMMIT` SQLite transaction.
2. Only *after* that transaction commits does the endpoint call `get_ai_feedback`, wrapped in `asyncio.to_thread` with a 6-second `asyncio.timeout`.
3. If the AI call succeeds within 6 seconds → `feedback` contains the string, `feedback_status = "ok"`.
4. If the AI call raises `AIFeedbackError`, any other exception, or exceeds the timeout → the `except Exception` block catches it, and the response returns `feedback = null`, `feedback_status = "unavailable"`. No retry is attempted.

**What a student actually sees:**

- **When `get_ai_feedback` is slow (but under 6s):** The HTTP response is delayed by however long the AI takes (0.5–5s), but the student gets their mastery score, milestone status, *and* feedback. The score is already committed, so even if the student's browser times out, their attempt is safe.
- **When `get_ai_feedback` is slow (over 6s) or raises `AIFeedbackError`:** The student gets a 200 response with their correct mastery score and milestone status, but `feedback` is `null` and `feedback_status` is `"unavailable"`. The student sees something like "Feedback is temporarily unavailable." Their attempt is fully recorded and their score is accurate; they just don't get the AI hint this time.

AI failure never rolls back or blocks the authoritative state change. The student's attempt, score, and milestone stay durable no matter what the AI does.

## 3. The crash-durability requirement

**Step-by-step scenario:** Student's mastery crosses 80 for a skill, but the process crashes before the milestone notification is recorded.

This *cannot happen* in my design because the attempt insertion, mastery update, and notification insertion all occur inside a **single SQLite transaction** using `BEGIN IMMEDIATE`:

```
BEGIN IMMEDIATE
  → INSERT INTO attempts (...)
  → INSERT/UPDATE mastery (...)
  → check: was previous_score ≤ 80 AND new_score > 80 AND no existing notification?
  → INSERT INTO notifications (...)
COMMIT
```

If the process crashes at any point *before* `COMMIT`, SQLite's WAL journal guarantees that on recovery, all three writes are rolled back atomically. The student's attempt, mastery update, and notification are **all-or-nothing**. You never get a mastery update to 82 without the corresponding notification row, and you never get a notification without the attempt being recorded.

If the process crashes *after* `COMMIT` but before the HTTP response reaches the client, the data is durable on disk (SQLite `PRAGMA synchronous=NORMAL` with WAL mode ensures the commit is fsynced). The student would need to retry, but since their mastery is already updated, a duplicate attempt would just produce another EWMA step. It wouldn't re-trigger the milestone because the `notifications` table already has the `UNIQUE(student_id, skill_id)` row.

**What I'd still worry about:**

- `PRAGMA synchronous=NORMAL` (not `FULL`) means there's a tiny window where a power failure (not just a process crash) could lose committed data in WAL mode. For a production system with real stakes, I'd use `synchronous=FULL` and accept the write latency, or switch to Postgres.
- The AI feedback call happens *after* the commit, so if the process crashes right after commit but before returning the response, the student never gets feedback for that attempt. There's no retry queue, so that feedback is lost for good. This is acceptable because feedback is explicitly best-effort, but a production system might want an async feedback pipeline.

## 4. Rate limiting

**Mechanism:** Sliding-window counter with a 24-hour (86,400-second) window and a cap of 30 attempts per student.

On every `POST /students/{id}/attempts`, inside the `BEGIN IMMEDIATE` transaction (before inserting the attempt), the system queries:

```sql
SELECT COUNT(*) FROM attempts
WHERE student_id = ? AND created_at > (now - 86400) AND created_at <= now
```

The window uses a half-open interval `(now - 86400, now]`. An attempt timestamped at *exactly* `now - 86400` is expired, not counted.

**Boundary behavior:**

- **Attempt #30:** Succeeds. The count query returns 29 (from the 29 existing attempts), which is `< 30`, so the 30th attempt is inserted. After this, there are 30 attempts in the window.
- **Attempt #31:** The count query returns 30, which is `≥ MAX_ATTEMPTS_PER_WINDOW`. The system computes `retry_after` by finding the oldest attempt in the window and calculating `ceil((oldest_created_at + 86400) - now)` (how many seconds until that oldest attempt expires from the window and frees a slot). A `RateLimitExceeded` exception is raised, which the FastAPI exception handler translates to a **429 response** with a `Retry-After` header and a JSON body containing `retry_after_seconds`. The rejected attempt is **not persisted** (the transaction rolls back), so it doesn't consume a rate-limit slot.
- **A request that fails validation (e.g. invalid `skill_id`, malformed body):** FastAPI's Pydantic validation rejects it with a 422 *before* the endpoint function even runs. No `record_attempt` call is made, so no attempt row is written and no rate-limit capacity is consumed. Similarly, authentication failures (401) and authorization failures (404) happen before the domain logic, so they don't touch the rate-limit window either.

The rate limit is **per-student, cross-skill**. It counts all attempts by a student regardless of which skill they're practicing.

## 5. If you had another 3 days

In priority order:

1. **Async feedback pipeline with retries.** Replace the synchronous-after-commit AI call with a background task queue (e.g. an `asyncio.Queue` or a lightweight job table in SQLite). Store a `feedback_request_id` on the attempt, poll/push results, and let the client fetch feedback separately via `GET /attempts/{id}/feedback`. This would decouple response latency from AI latency entirely.

2. **Observability.** Add structured logging (request IDs, latency metrics, AI success/failure rates), a health-check endpoint with database connectivity status, and basic Prometheus-style metrics for rate-limit hits, milestone events, and P95 response times.

3. **Pagination and filtering on GET endpoints.** The mastery and notifications endpoints currently return all data. For a student with hundreds of milestones across many skills, these need cursor-based pagination and skill-id filtering.

4. **Per-skill or adaptive rate limiting.** The current 30/24h global cap is blunt. A student cramming one difficult skill shouldn't be blocked from practicing others. Per-skill sub-limits or an adaptive policy that raises limits for students showing genuine study patterns would be more pedagogically useful.

5. **Mastery history / audit trail.** Store mastery snapshots over time (score at each attempt) so teachers can see learning trajectories, not just the current score. This also enables analytics on which skills have the steepest decay or the slowest improvement.

6. **Integration tests with real AI latency profiles.** The current tests monkeypatch the AI provider. Add a small integration suite that runs against the real `get_ai_feedback` (sleeps and failure rate included) to verify timeout behavior and response-time SLAs.

## 6. Least confident about

The **mastery scoring formula** itself. The EWMA + half-life approach works mechanically and the math is correct, but whether `α = 0.20`, `HALF_LIFE_DAYS = 30`, and `BASELINE_SEED = 50` are pedagogically reasonable is something I can't validate without real student data. 50 as a cold-start score is a guess. It means a brand-new student on an untouched skill shows "50% mastery" before they've ever answered a question, which could be misleading. Starting at 0 and requiring students to build up might be more honest, but then the EWMA climb to 80 takes more attempts and could feel discouraging.

I'm also somewhat unsure about the `synchronous=NORMAL` pragma choice. It's the right trade-off for a take-home (faster writes, crash-safe for process crashes), but in a follow-up conversation I'd want to discuss whether the assignment expected `synchronous=FULL` for maximum durability guarantees.

## 7. AI tool use

I used an AI coding assistant (Claude) throughout this project for:

- **Scaffolding and boilerplate:** Initial FastAPI project structure, Pydantic schema definitions, Dockerfile, and the SQLite connection setup. I accepted most of this as-is since it's standard plumbing.
- **Test generation:** The assistant generated initial test cases for each module. I reworked several of them — particularly the concurrency tests (`test_begin_immediate_writer_reservation`), where the AI's first version didn't properly set `busy_timeout=0` on the second connection, making the locking assertion flaky. I also rewrote the AI feedback ordering tests to use `monkeypatch` on the module-level import rather than trying to mock `asyncio.to_thread` directly, which was fragile.
- **Scoring formula exploration:** I discussed EWMA vs. Bayesian approaches with the assistant and settled on EWMA for simplicity. The decay-toward-zero (not toward baseline) was a deliberate correction I made after the AI initially suggested decaying toward `BASELINE_SEED`, which would have meant students who stop practicing never drop below 50.
- **Rate-limit boundary logic:** The assistant helped draft the sliding-window SQL query, but I had to fix the window boundary from `>=` to `>` for the lower bound to get the half-open interval semantics right. Attempts exactly at `now - WINDOW_SECONDS` should be expired, not counted.
