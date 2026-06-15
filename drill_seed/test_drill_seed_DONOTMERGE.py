"""SEEDED-BAD-PR DRILL — DO NOT MERGE. Deliberately failing test to prove `ci` goes RED."""


def test_intentionally_failing_drill():
    # This MUST fail — it is the seeded `ci`-red component of the greploop posture-B drill.
    assert 1 == 2, "seeded drill failure (expected): proves CI floor reports red"
