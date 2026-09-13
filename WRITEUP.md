<!--
Fill this in and submit it alongside your code. Answer in your own words —
these are the questions we'll also ask you about in the follow-up
conversation, so they should match what your code actually does.
-->

# Writeup

## 1. Mastery scoring

What formula/approach did you use to turn a sequence of correct/incorrect
attempts into a 0-100 mastery score? Why this one, and what does it get
wrong that a better version would fix?

## 2. The flaky AI dependency

How does your `/attempts` endpoint behave when `get_ai_feedback` is slow?
When it raises `AIFeedbackError`? What would a student actually see in each
case?

## 3. The crash-durability requirement

Walk through what happens, step by step, if the process crashes right after
a student's mastery crosses 80 for a skill but before the milestone
notification is recorded. What guarantees does your design actually give,
and what would you still worry about?

## 4. Rate limiting

How does your 30-attempts/24h limit work? What happens right at the
boundary (attempt #30, attempt #31, a request that fails validation)?

## 5. If you had another 3 days

What would you build or fix next, in priority order?

## 6. Least confident about

Which part of this submission are you least sure is correct or
well-designed? (This isn't a trick question — we'd rather you tell us than
we find out later.)

## 7. AI tool use

Which parts, if any, did you use an AI coding assistant for? What did you
have to fix, reject, or rework from what it gave you?
