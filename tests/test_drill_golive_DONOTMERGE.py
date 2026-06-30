"""DRILL SEED — DELETE. Go-live floor proof: a deterministically failing test
so the ci floor component goes RED. Asserts a falsehood on purpose."""


def test_drill_golive_intentional_failure():
    # Intentional RED to prove the ci floor blocks a broken PR end-to-end.
    assert 1 == 2, "seeded-bad-PR drill: this failure is intentional"
