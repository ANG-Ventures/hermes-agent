"""END-TO-END EFFECT test: a sandbox script's comment carries its session.

Companion to ``tests/tools/test_kanban_comment_provenance_tools.py`` (which
covers the in-process ``kanban_comment`` tool). That surface was already
correct; this file covers the surface that was NOT — the ``execute_code``
sandbox, whose child env is built by ``_scrub_child_env``.

🔴 Why an EFFECT test and not a resolver test. Comment provenance shipped with
41 passing tests and attributed 0 of 723 live comments. Every one of those tests
handed the resolver an env var and asserted it returned a fingerprint — which is
true, and says nothing about whether the value ever reaches the process that
writes the row. The measured production failure was exactly that gap: the
orchestrator wrote its comments by shelling out to ``hermes kanban comment``
from inside ``execute_code``, and ``_scrub_child_env``'s allowlist dropped
``HERMES_SESSION_ID`` on the way into the child. In-process tool call →
attributed; sandbox script in the SAME turn → NULL.

So these tests spawn a REAL child process, run the REAL CLI entry point against
a REAL sqlite board, and assert on the persisted ROWS. ``test_two_concurrent_..``
is the acceptance gate: two same-profile sessions must produce two DISTINCT
non-null ``session_ref`` values.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def _reset_session_id_contextvar():
    """A contextvar left bound by another test file would shadow the env these
    tests set (the resolver is contextvar-first)."""
    from gateway.session_context import _SESSION_ID, _UNSET

    _SESSION_ID.set(_UNSET)
    yield
    _SESSION_ID.set(_UNSET)


@pytest.fixture
def board(monkeypatch, tmp_path):
    """A real, sandboxed kanban DB with one task. Returns ``(task_id, home)``.

    Hermeticity: ``kanban_db_path()`` consults ``HERMES_KANBAN_DB`` BEFORE
    anything ``HERMES_HOME``-derived, and the dispatcher pins that var into
    every worker env — so running this suite inside a worker without the
    ``delenv`` below resolves to the LIVE production board.
    """
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_PROFILE", "apollo")
    monkeypatch.delenv("HERMES_SESSION_ID", raising=False)
    for var in [k for k in os.environ if k.startswith("HERMES_KANBAN")]:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    from hermes_cli import kanban_db as kb

    kb._INITIALIZED_PATHS.clear()
    resolved = kb.kanban_db_path()
    assert str(resolved).startswith(str(tmp_path)), f"DB escaped sandbox: {resolved}"
    assert "/.hermes/kanban/boards/" not in str(resolved), (
        f"DB resolved onto a live board: {resolved}"
    )
    kb.init_db()
    conn = kb.connect()
    try:
        task_id = kb.create_task(conn, title="e2e provenance", assignee="apollo")
    finally:
        conn.close()
    yield task_id, home
    kb._INITIALIZED_PATHS.clear()


def _comments(task_id):
    from hermes_cli import kanban_db as kb

    conn = kb.connect()
    try:
        return kb.list_comments(conn, task_id)
    finally:
        conn.close()


def _sandbox_child_env(session_id):
    """The env a sandbox child ACTUALLY receives, built by the production path.

    Binds the session the way the gateway does (contextvar) rather than writing
    ``os.environ`` — that is the arrangement under which the process-global is
    NOT authoritative, so a bridge that reads ``os.environ`` instead of the
    resolver would fail this.
    """
    from gateway.session_context import clear_session_vars, set_session_vars
    from tools.code_execution_tool import _scrub_child_env

    tokens = set_session_vars(
        platform="discord",
        chat_id="c1",
        session_key=f"discord:{session_id}",
        session_id=session_id,
        profile="apollo",
        cron_session="",
    )
    try:
        return _scrub_child_env(dict(os.environ))
    finally:
        clear_session_vars(tokens)


def _cli_argv(task_id, body):
    """The real ``hermes kanban comment …`` invocation, as a child argv.

    Production shells out to the ``hermes`` console script; we invoke the same
    ``hermes_cli.main`` entry point via ``-m`` so the test does not depend on a
    console script being installed on PATH in CI.
    """
    return [sys.executable, "-m", "hermes_cli.main", "kanban", "comment", task_id, body]


def _comment_from_sandbox(task_id, session_id, body, *, home):
    """Write a comment the way production does: a CHILD PROCESS running the
    real CLI, with the env ``_scrub_child_env`` produced.

    Returns the child's ``CompletedProcess`` so callers can assert it succeeded
    — a silently-failing write would otherwise read as "no provenance".
    """
    child_env = _sandbox_child_env(session_id)
    # The real caller re-adds the repo to PYTHONPATH after scrubbing (see
    # _execute_local); mirror that so the child can import hermes_cli.
    child_env["PYTHONPATH"] = str(REPO)
    child_env["HERMES_HOME"] = str(home)
    child_env["HOME"] = str(home.parent)
    return subprocess.run(
        _cli_argv(task_id, body),
        env=child_env,
        capture_output=True,
        text=True,
        timeout=180,
        cwd=str(REPO),
    )


# ---------------------------------------------------------------------------
# AC — two concurrent same-profile sessions are distinguishable ON THE ROWS
# ---------------------------------------------------------------------------


def test_two_concurrent_same_profile_sandbox_sessions_are_distinguishable(board):
    """THE acceptance gate. Two same-profile sessions each write a comment
    through the real sandbox → child-process → CLI → sqlite path; the persisted
    rows must carry two DISTINCT non-null ``session_ref`` values.

    Before the child-env bridge both rows were NULL/NULL — the exact live
    symptom (0 of 723 comments attributed), with the resolver itself working
    perfectly the whole time.
    """
    task_id, home = board

    a = _comment_from_sandbox(task_id, "apollo-session-A", "parking this", home=home)
    b = _comment_from_sandbox(task_id, "apollo-session-B", "waiver granted", home=home)
    assert a.returncode == 0, f"child A failed: {a.stdout}\n{a.stderr}"
    assert b.returncode == 0, f"child B failed: {b.stdout}\n{b.stderr}"

    rows = _comments(task_id)
    assert len(rows) == 2, rows
    refs = [r.session_ref for r in rows]
    assert all(refs), f"sandbox comment landed with NULL provenance: {refs}"
    assert refs[0] != refs[1], f"two sessions collapsed to one fingerprint: {refs}"


def test_sandbox_session_ref_matches_the_in_process_tool(board, monkeypatch):
    """Cross-surface agreement: the same turn must attribute identically
    whichever surface writes the comment.

    This is what makes the fingerprint usable for correlation — if the sandbox
    hashed a different value than the tool, two writes from ONE session would
    look like two sessions, which is the same ambiguity in a new costume.
    """
    from hermes_cli import kanban_db as kb

    task_id, home = board
    proc = _comment_from_sandbox(task_id, "apollo-session-A", "from sandbox", home=home)
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"

    rows = _comments(task_id)
    assert len(rows) == 1
    assert rows[0].session_ref == kb.derive_session_ref("apollo-session-A")


# ---------------------------------------------------------------------------
# Fail-open — a session with no id still writes the comment
# ---------------------------------------------------------------------------


def test_unresolvable_session_still_writes_the_comment(board, monkeypatch):
    """Provenance is an ANNOTATION, never an admission gate. No session id →
    the comment still lands, with NULL provenance that renders as an explicit
    unknown rather than a guess."""
    from hermes_cli import kanban_db as kb

    task_id, home = board

    from tools.code_execution_tool import _scrub_child_env

    monkeypatch.delenv("HERMES_SESSION_ID", raising=False)
    child_env = _scrub_child_env(dict(os.environ))
    assert "HERMES_SESSION_ID" not in child_env
    child_env["PYTHONPATH"] = str(REPO)
    child_env["HERMES_HOME"] = str(home)
    child_env["HOME"] = str(home.parent)

    proc = subprocess.run(
        _cli_argv(task_id, "no session here"),
        env=child_env,
        capture_output=True,
        text=True,
        timeout=180,
        cwd=str(REPO),
    )
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"

    rows = _comments(task_id)
    assert len(rows) == 1
    assert rows[0].body == "no session here"
    assert rows[0].session_ref is None
    assert rows[0].run_id is None
    # And the legacy render stays honest for the NULL row.
    assert kb.format_comment_author(rows[0].author) == "apollo (provenance unknown)"


# ---------------------------------------------------------------------------
# The bridge itself — unit-level guards on the properties the AC depends on
# ---------------------------------------------------------------------------


def test_child_env_carries_the_bound_session_not_the_process_global(monkeypatch):
    """🔴 The gateway-concurrency property.

    Inside the gateway the ``os.environ`` mirror is last-writer-wins across
    concurrent sessions. A bridge that copied it (e.g. by adding the var to
    ``_HERMES_CHILD_ALLOWED``) would attribute this sandbox to whichever session
    wrote the global most recently. Bind session B in the contextvar while the
    global says A, and require B.
    """
    from tools.code_execution_tool import _scrub_child_env

    monkeypatch.setenv("_HERMES_GATEWAY", "1")
    monkeypatch.setenv("HERMES_SESSION_ID", "FOREIGN-session-A")

    from gateway.session_context import clear_session_vars, set_session_vars

    tokens = set_session_vars(session_id="my-session-B", cron_session="")
    try:
        env = _scrub_child_env(dict(os.environ))
    finally:
        clear_session_vars(tokens)

    assert env["HERMES_SESSION_ID"] == "my-session-B"


def test_cleared_gateway_context_strips_rather_than_leaking_the_global(monkeypatch):
    """Post-turn (``clear_session_vars`` → ``""``) inside the gateway, the
    process-global is deliberately suppressed. The child must get NO session id
    rather than a foreign one — an unattributed comment beats a misattributed
    one."""
    from tools.code_execution_tool import _scrub_child_env

    monkeypatch.setenv("_HERMES_GATEWAY", "1")
    monkeypatch.setenv("HERMES_SESSION_ID", "FOREIGN-session-A")

    from gateway.session_context import clear_session_vars, set_session_vars

    clear_session_vars(set_session_vars(session_id="gone", cron_session=""))
    env = _scrub_child_env(dict(os.environ))

    assert "HERMES_SESSION_ID" not in env


def test_single_process_host_uses_os_environ(monkeypatch):
    """A dispatcher-spawned worker / CLI / cron-standalone has no bound
    contextvar and ``os.environ`` IS correct there (no concurrency to leak
    across). The bridge must not break that case while fixing the gateway one.
    """
    from tools.code_execution_tool import _scrub_child_env

    monkeypatch.delenv("_HERMES_GATEWAY", raising=False)
    monkeypatch.setenv("HERMES_SESSION_ID", "worker-session-id")

    env = _scrub_child_env(dict(os.environ))

    assert env["HERMES_SESSION_ID"] == "worker-session-id"


def test_session_id_is_not_in_the_static_allowlist():
    """Guard against the tempting one-line 'fix'.

    Adding ``HERMES_SESSION_ID`` to ``_HERMES_CHILD_ALLOWED`` passes it straight
    from ``os.environ`` — which makes the two gateway tests above regress while
    the AC test still passes, since a single-session test can't tell the two
    sources apart. Pin the mechanism, not just the outcome.
    """
    from tools.code_execution_tool import _HERMES_CHILD_ALLOWED

    assert "HERMES_SESSION_ID" not in _HERMES_CHILD_ALLOWED


def test_remote_backend_passes_the_session_id_shell_quoted():
    """The remote/file-RPC spawn path builds its env as a shell prefix, not a
    dict — a separate site that must carry the same identity (and must quote
    it, same as TZ)."""
    import shlex

    from tools.code_execution_tool import _resolved_session_id
    from gateway.session_context import clear_session_vars, set_session_vars

    tokens = set_session_vars(session_id="remote-session-A", cron_session="")
    try:
        resolved = _resolved_session_id()
    finally:
        clear_session_vars(tokens)

    assert resolved == "remote-session-A"
    assert shlex.quote(resolved) == "remote-session-A"
