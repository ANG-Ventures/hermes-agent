"""sqlite_plan_guard: catch a query whose plan makes SQLite spill whole rows to a temp file.

Ported from hermes-home scripts/lib/sqlite_plan_guard.py (t_ff895cd1, hermes-home#12492); hermes-agent
tests cannot import that repo. A plan step ``USE TEMP B-TREE`` (an ORDER BY / GROUP BY / DISTINCT no index
serves) or an ``AUTOMATIC`` index writes its intermediate result to an etilqs_* temp file once it outgrows
the page cache (default 2 MB). On the Studio those spills were tens of GB/day (t_d24713d3).

``outer_only=True`` checks only the statement's own top-level steps: a sort there sorts the full output
rows, while a sort inside a subquery sorts that subquery's (narrow) projection. Use it for the
"rank keys in a subquery, join full rows by primary key" shape.

    rec = sqlite_plan_guard.Recorder(con)
    code_under_test(rec)                         # anything that only calls .execute / .executemany
    assert sqlite_plan_guard.spills(con, rec.statements) == []

Plans come from EXPLAIN QUERY PLAN on the same connection, so the fixture must carry the production indexes.
"""
from __future__ import annotations

SPILL_STEPS = ("USE TEMP B-TREE", "AUTOMATIC")


class Recorder:
    """A stand-in connection that records every (sql, params) it executes and delegates to the real one."""

    def __init__(self, con):
        self._con = con
        self.statements = []

    def execute(self, sql, params=()):
        self.statements.append((sql, tuple(params)))
        return self._con.execute(sql, params)

    def executemany(self, sql, seq):
        return self._con.executemany(sql, seq)

    def __getattr__(self, name):
        return getattr(self._con, name)


def spill_steps(con, sql, params=(), *, outer_only=False):
    """The EXPLAIN QUERY PLAN lines of `sql` that spill to a temp file ([] = none)."""
    return [row[-1] for row in con.execute("EXPLAIN QUERY PLAN " + sql, tuple(params))
            if any(s in row[-1] for s in SPILL_STEPS) and (not outer_only or row[1] == 0)]


def spills(con, statements, *, outer_only=False):
    """[(sql, [spill steps])] for every SELECT/WITH statement in `statements` whose plan spills."""
    out = []
    for sql, params in statements:
        if sql.lstrip().split(None, 1)[0].upper() not in ("SELECT", "WITH"):
            continue
        steps = spill_steps(con, sql, params, outer_only=outer_only)
        if steps:
            out.append((sql, steps))
    return out
