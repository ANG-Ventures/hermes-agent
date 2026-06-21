"""SEEDED-BAD DRILL — DO NOT MERGE. Deliberately failing test to drive a `ci` floor-red
for the greploop live seeded-bad-PR drill (PRD §9 / WS1 step e). Deleted at drill end."""


def test_greploop_drill_intentional_failure():
    # This MUST fail so the `test (N)` shard goes RED -> ci floor component red.
    assert 1 == 2, "intentional drill failure (greploop seeded-bad drill — not a real bug)"
