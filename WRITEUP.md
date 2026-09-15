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

**Precision and representation:** The database persists the authoritative score checkpoint `(score, last_practiced_at)` as an exact, unrounded floating-point number so subsequent decay and EWMA updates do not compound quantization errors. However, both `GET /students/{id}/mastery` and `POST /students/{id}/attempts` round the returned score to **2 decimal places** (`round(score, 2)`). Because continuous time-based decay produces microsecond-level decimal drift (e.g. `80.00` decaying to `79.9987...` over 60 seconds with zero student attempts), rounding avoids exposing floating-point noise or creating an impression of arbitrary score jitter on teacher dashboards while preserving exact math under the hood.

**Why this approach:** EWMA is simple, deterministic, and gives recent attempts more weight than older ones without needing to store or replay the full attempt history. The mastery table only holds `(score, last_practiced_at)`. The half-life decay models the real phenomenon that skills fade without practice, and decaying toward 0 (not toward 50) means a student who stops practicing will eventually show near-zero mastery rather than settling at an ambiguous midpoint.

**What it gets wrong / what a better version would fix:**

- The α and half-life constants are hardcoded guesses. A production system would calibrate these from real student performance data, possibly per-skill or per-difficulty-tier.
- A single scalar score doesn't distinguish *consistency* from *recent lucky streak*. A student who gets 5 right in a row after 20 wrong looks the same as one who has been steadily improving. A better model might track a confidence interval or use a Bayesian approach (e.g. BKT or Elo-style rating) that separates "likely knows" from "uncertain".
- The decay model is purely time-based and doesn't account for spaced-repetition effects. Ideally, a skill practiced in well-spaced intervals should decay slower than one crammed in a single session.
- α = 0.20 means each attempt shifts the score by at most 20 points, so climbing from 50 to 80+ takes a minimum of ~3 consecutive correct answers. That might be too lenient or too strict depending on the skill.

## 2. The flaky AI dependency

The `/attempts` endpoint treats AI feedback as **best-effort enrichment** that is strictly decoupled from the core domain transaction. The sequence:

1. The domain transaction (`_record_attempt_in_worker`) runs first off the asyncio event loop via `asyncio.to_thread` — connection creation, rate-limit check, attempt insertion, mastery update, milestone notification, and commit all happen inside an isolated worker thread.
2. Only *after* that transaction commits does the endpoint invoke `fetch_ai_feedback`.
3. Concurrency is bounded by a **dedicated 16-worker `ThreadPoolExecutor`** guarded by a non-blocking semaphore (`ai_semaphore.acquire(blocking=False)`):
   - **Immediate degradation on saturation:** If all 16 AI worker slots are occupied, the endpoint does *not* queue work in an unbounded executor queue. It immediately degrades and returns `feedback = null`, `feedback_status = "unavailable"`, shielding the HTTP response from queue delay.
   - **Strict 6-second timeout:** If an AI slot is acquired, the call is awaited under `asyncio.timeout(6)`.
   - **Orphaned thread slot retention:** Python cannot forcibly terminate an OS thread mid-sleep or mid-network I/O. When the 6-second timeout fires, the HTTP endpoint returns `feedback_status = "unavailable"` immediately to the client, but the worker slot remains reserved until the underlying thread finishes (slot release is deferred to the thread's `finally:` block). This prevents timed-out threads from silently oversubscribing capacity.
4. If the AI call succeeds within 6 seconds → `feedback` contains the string, `feedback_status = "ok"`.
5. If the AI call raises `AIFeedbackError`, any other exception, or exceeds the timeout → the exception is caught, and the response returns `feedback = null`, `feedback_status = "unavailable"`. No retry is attempted. Replayed idempotent attempts bypass AI feedback entirely.

**What a student actually sees:**

- **When `get_ai_feedback` is slow (but under 6s) and capacity is available:** The HTTP response is delayed by however long the AI takes (0.5–5s), but the student gets their mastery score, milestone status, *and* feedback. The score is already committed, so even if the student's browser times out, their attempt is safe.
- **When `get_ai_feedback` is slow (over 6s), raises an error, or AI capacity is saturated:** The student gets a 200 response with their correct mastery score and milestone status, but `feedback` is `null` and `feedback_status` is `"unavailable"`. The student sees something like "Feedback is temporarily unavailable." Their attempt is fully recorded and their score is accurate; they just don't get the AI hint this time.

AI failure or saturation never rolls back or blocks the authoritative state change. The student's attempt, score, and milestone stay durable no matter what the AI does.

## 3. The crash-durability requirement

**Step-by-step scenario:** Student's mastery crosses 80 for a skill, but the process crashes before the milestone notification is recorded.

This *cannot happen* in my design because the attempt insertion, mastery update, notification insertion, and idempotency key recording all occur inside a **single SQLite transaction** using `BEGIN IMMEDIATE`:

```
BEGIN IMMEDIATE
  → if idempotency_key given: check idempotency_keys
      → if existing matches: COMMIT and return stored result (replay)
      → if existing conflicts: COMMIT and raise 409 Conflict
  → rate limit check
  → INSERT INTO attempts (...)
  → INSERT/UPDATE mastery (...)
  → check: was previous_score ≤ 80 AND new_score > 80 AND no existing notification?
  → INSERT INTO notifications (...)
  → if idempotency_key given: INSERT INTO idempotency_keys (...)
COMMIT
```

**Off-loop thread architecture:** In `submit_attempt` (`async def`), the entire database transaction runs in a worker thread via `asyncio.to_thread(_record_attempt_in_worker)`. A dedicated SQLite connection is created, used, and closed within the same worker thread. This keeps the transaction strictly isolated and prevents blocking operations (such as waiting on `PRAGMA busy_timeout=5000` during lock contention) from stalling the asyncio event loop or delaying concurrent HTTP requests (e.g. `/health`).

If the process crashes at any point *before* `COMMIT`, SQLite's WAL journal guarantees that on recovery, all writes are rolled back atomically. The student's attempt, mastery update, notification, and idempotency record are **all-or-nothing**. You never get a mastery update to 82 without the corresponding notification row, and you never get a notification without the attempt being recorded.

If the process crashes *after* `COMMIT` but before the HTTP response reaches the client, the data is durable on disk (SQLite `PRAGMA synchronous=NORMAL` with WAL mode ensures the commit is fsynced). Because client retries can send an `Idempotency-Key` header:
- On retry with matching `(skill_id, is_correct)`, the endpoint detects the existing `(student_id, idempotency_key)` row, bypasses the rate limiter, does not insert a duplicate attempt or recompute mastery, and returns the original stored result (`mastery`, `milestone_reached`). Replays explicitly skip AI feedback (`feedback = null`, `feedback_status = "unavailable"`).
- If the client retries with conflicting parameters for the same key, it receives a 409 Conflict without modifying any data.
- If no `Idempotency-Key` header was provided, a retry still wouldn't re-trigger the milestone because the `notifications` table has a `UNIQUE(student_id, skill_id)` constraint, though it would apply another EWMA step.

**What I'd still worry about:**

- `PRAGMA synchronous=NORMAL` (not `FULL`) means there's a tiny window where a power failure (not just a process crash) could lose committed data in WAL mode. For a production system with real stakes, I'd use `synchronous=FULL` and accept the write latency, or switch to Postgres.
- The AI feedback call happens *after* the commit, so if the process crashes right after commit but before returning the response, the student never gets feedback for that attempt. There's no retry queue, so that feedback is lost for good. This is acceptable because feedback is explicitly best-effort, but a production system might want an async feedback pipeline.
- SQLite write concurrency is fundamentally serialized at the file lock level. While offloading to worker threads protects event loop liveness, high write volume will still queue on the single writer lock, which Postgres row-level locking would solve.

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
- **Idempotent replay requests:** When an attempt request arrives with an already-recorded `Idempotency-Key` and matching parameters, the domain transaction commits and returns immediately upon finding the key in `idempotency_keys`. It never queries or increments the rolling rate-limit window. A student can replay an existing attempt indefinitely without burning any of their 30-attempt quota.

The rate limit is **per-student, cross-skill**. It counts all attempts by a student regardless of which skill they're practicing.

## 5. If you had another 3 days

In priority order:

1. **Async feedback pipeline with retries.** While AI concurrency is now bounded to 16 workers with immediate degradation, feedback is still synchronous-after-commit. Replace this with a background task queue (e.g. an `asyncio.Queue` or a lightweight job table in SQLite). Store a `feedback_request_id` on the attempt, poll/push results, and let the client fetch feedback separately via `GET /attempts/{id}/feedback`. This would decouple response latency from AI latency entirely.

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
