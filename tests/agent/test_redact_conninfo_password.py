"""libpq conninfo password= in terminal output (t_7a36b3f0).

`docker exec <pg> cat $PGDATA/postgresql.auto.conf` printed both mem0 replication passwords
verbatim: primary_conninfo carries a mid-line lowercase password=, which no other pass masks.
"""
import pytest

from agent.redact import redact_terminal_output

PW = "Zq9vXbT3kLmN8pRsW2yA"  # synthetic
CMD = "docker exec mem0-selfhost-postgres-1 sh -c 'cat $PGDATA/postgresql.auto.conf'"


@pytest.mark.parametrize("line", [
    f"primary_conninfo = 'user=mem0_repl password={PW} channel_binding=prefer host=primary-link port=5432'",
    f"primary_conninfo = 'user=mem0_repl password=''{PW}'' host=primary-link'",
    f"primary_conninfo = 'user=mem0_repl password=''a b{PW}'' host=primary-link'",
    f"primary_conninfo = 'user=mem0_repl password=\\'{PW}\\' host=primary-link'",
    f"psql 'host=db user=x password={PW} dbname=y'",
    f"PASSWORD={PW} user=x host=y",
])
def test_conninfo_password_is_masked(line):
    out = redact_terminal_output(line, CMD, force=True)
    assert PW not in out
    assert "password=***" in out.lower()


def test_conninfo_keeps_the_other_keywords():
    line = f"primary_conninfo = 'user=mem0_repl password={PW} host=primary-link port=5432'"
    assert redact_terminal_output(line, CMD, force=True) == \
        "primary_conninfo = 'user=mem0_repl password=*** host=primary-link port=5432'"


def test_prose_password_without_conninfo_keywords_is_left_alone():
    line = "set password=hunter2 in the form and submit"
    assert redact_terminal_output(line, "echo", force=True) == line


def test_passfile_conninfo_has_nothing_to_mask():
    line = "primary_conninfo = 'user=mem0_repl passfile=/var/lib/postgresql/data/.mem0-repl.pgpass host=primary-link'"
    assert redact_terminal_output(line, CMD, force=True) == line
