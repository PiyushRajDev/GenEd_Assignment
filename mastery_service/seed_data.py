"""
GIVEN — seed data and the fake auth map. Do not build a real signup/login
flow; that's explicitly out of scope for this project (see PROBLEM.md).

HOW AUTH WORKS FOR THIS PROJECT:
Every request carries a header:  Authorization: Bearer <token>
Tokens below map directly to a (role, user_id) pair — there is no JWT to
decode, no password, no expiry. Look up the token, get the identity, apply
the role rules described in PROBLEM.md. That's the whole auth system for
this exercise.
"""

TOKENS = {
    "token-student-ananya": {"role": "STUDENT", "user_id": "student-ananya"},
    "token-student-rohan": {"role": "STUDENT", "user_id": "student-rohan"},
    "token-student-mei": {"role": "STUDENT", "user_id": "student-mei"},
    "token-teacher-kavita": {"role": "TEACHER", "user_id": "teacher-kavita"},
}

# Which students each teacher can view. A teacher may only view students on
# their own roster.
TEACHER_ROSTERS = {
    "teacher-kavita": ["student-ananya", "student-rohan"],
    # Note: student-mei is deliberately NOT on any roster — she's an
    # unassigned/new student. Your access rules should still make sense here.
}

STUDENT_IDS = ["student-ananya", "student-rohan", "student-mei"]

SKILL_IDS = [
    "math.fractions.add-subtract",
    "math.fractions.multiply-divide",
    "math.algebra.linear-equations",
    "science.physics.newtons-laws",
]
