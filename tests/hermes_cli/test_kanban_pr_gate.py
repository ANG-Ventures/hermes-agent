"""PR-gate re-evaluator: a card blocked on "merge PR #N" resumes when #N merges.

Context (2026-09-21 board sweep): six ``needs_input`` cards sat blocked for up
to 12 hours on gates whose stated blocker was "merge PR #N then unblock me" —
every referenced PR had already merged. Nothing in the dispatcher tick
re-evaluated a block whose premise is an EXTERNAL object, so the class was only
ever caught by a human board sweep.

These tests pin:

* the reference parser truth table (bare ``#N`` with/without repo context,
  ``owner/repo#N``, full URL, multiple refs, zero refs, ambiguous context),
* the state machine (all-merged -> unblock; any-open -> hold; closed-unmerged ->
  comment only; lookup error -> no-op),
* the per-tick lookup cap and the cross-tick result cache,
* scope fencing (only ``needs_input``/``capability``/``dependency`` blocks; never
  a block whose reason names no PR),
* and one end-to-end pass against a real sqlite board with a stubbed ``gh``.
"""

from __future__ import annotations

import contextlib
import json
import subprocess
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect
from hermes_cli import kanban_db_dispatch
from hermes_cli import kanban_pr_gate as prg


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolated HERMES_HOME with an empty kanban DB."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


@pytest.fixture(autouse=True)
def _clear_pr_cache() -> None:
    prg.clear_cache()
    yield
    prg.clear_cache()


def _merged(sha: str = "abcdef1234567890", at: str = "2026-09-20T13:05:00Z") -> dict:
    return {"state": "MERGED", "mergedAt": at, "mergeCommitSha": sha}


def _open_pr() -> dict:
    return {"state": "OPEN", "mergedAt": None, "mergeCommitSha": None}


def _closed() -> dict:
    return {"state": "CLOSED", "mergedAt": None, "mergeCommitSha": None}


def _stub(mapping, *, calls=None):
    """Build a ``query_fn`` over ``{(repo, number): payload-or-None}``."""

    def query_fn(repo: str, number: int):
        if calls is not None:
            calls.append((repo, number))
        return mapping.get((repo, number))

    return query_fn


# ---------------------------------------------------------------------------
# Parser truth table
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text,default_repo,expected",
    [
        # Bare #N with repo context resolves against it.
        ("merge PR #787 then unblock me", "ANG-Ventures/hermes-agent",
         [("ANG-Ventures/hermes-agent", 787)]),
        # Bare #N with NO repo context is unresolvable -> dropped.
        ("merge PR #787 then unblock me", None, []),
        # owner/repo#N carries its own context and wins over the default.
        ("blocked on NousResearch/hermes-agent#4211", "ANG-Ventures/hermes-agent",
         [("NousResearch/hermes-agent", 4211)]),
        # Full URL.
        ("waiting on https://github.com/ANG-Ventures/hermes-agent/pull/790", None,
         [("ANG-Ventures/hermes-agent", 790)]),
        # URL with trailing path/punctuation (JSON handbacks abut a quote+comma).
        ('{"pr": "https://github.com/o/r/pull/12/files",}', None, [("o/r", 12)]),
        # bare pull/N against context.
        ("gate: pull/55 must land", "o/r", [("o/r", 55)]),
        # Multiple refs, de-duplicated and ordered.
        ("needs #5 and o/r#9 and #5 again", "o/r",
         [("o/r", 5), ("o/r", 9)]),
        # Zero PR references.
        ("blocked: need Ace to decide the retention window", "o/r", []),
        # A task id is not a PR reference.
        ("blocked on t_3c0420ad finishing", "o/r", []),
        # Case-insensitive repo, normalized.
        ("ANG-Ventures/Hermes-Agent#1", None, [("ANG-Ventures/Hermes-Agent", 1)]),
        # #0 is not a valid PR number.
        ("merge PR #0", "o/r", []),
    ],
)
def test_parse_pr_refs_truth_table(text, default_repo, expected) -> None:
    refs = prg.parse_pr_refs(text, default_repo=default_repo)
    assert [(r.repo, r.number) for r in refs] == expected


def test_parse_pr_refs_ignores_non_string() -> None:
    assert prg.parse_pr_refs(None, default_repo="o/r") == []
    assert prg.parse_pr_refs("", default_repo="o/r") == []


def test_repo_context_prefers_single_workspace_remote(tmp_path: Path) -> None:
    repo = tmp_path / "wt"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(
        ["git", "remote", "add", "origin", "git@github.com:o/r.git"],
        cwd=repo, check=True,
    )
    assert prg.repo_context(workspace_path=str(repo), body=None) == "o/r"


def test_repo_context_falls_back_to_body_mention(tmp_path: Path) -> None:
    assert prg.repo_context(
        workspace_path=str(tmp_path / "missing"),
        body="land it on ANG-Ventures/hermes-agent first",
    ) == "ANG-Ventures/hermes-agent"


@pytest.mark.parametrize(
    "body",
    [
        # Reviewer round-3 case 1: two-segment source path before the repo.
        (
            "BUILD: edit hermes_cli/kanban.py. "
            "Fork-first ANG-Ventures/hermes-agent. Merge PR #808."
        ),
        # Reviewer round-3 case 2: nested test path before the repo.
        (
            "Tests in tests/hermes_cli/test_x.py; "
            "repo ANG-Ventures/hermes-agent; merge #808."
        ),
        # THIS card's own body: a GLOB path, whose extension a suffix
        # denylist never sees because ``*`` truncates the match.
        (
            "BUILD: in the kanban dispatcher tick (hermes-agent, "
            "hermes_cli/kanban*.py - find the tick that runs "
            "recompute_ready). Fork-first ANG-Ventures/hermes-agent."
        ),
        # Underscored module dir with no extension at all.
        (
            "patch hermes_cli/kanban_db then land on "
            "ANG-Ventures/hermes-agent; merge #808."
        ),
    ],
)
def test_repo_context_ignores_source_paths_before_body_repo(
    tmp_path: Path, body: str
) -> None:
    """A GitHub *owner* never contains ``_`` or ``.``; a module path does.

    The discriminator must be a positive property of the slug, not a
    denylist of file extensions: ``hermes_cli/kanban*.py`` truncates to
    ``hermes_cli/kanban`` (no suffix to deny) and ``hermes_cli/kanban_db``
    never had one.
    """
    assert prg.repo_context(
        workspace_path=str(tmp_path / "missing"), body=body
    ) == "ANG-Ventures/hermes-agent"


def test_repo_context_is_none_when_body_has_two_plausible_slugs(
    tmp_path: Path,
) -> None:
    """``src/utils`` is a *legal* repo slug, so it cannot be ruled out.

    Two uncorroborated candidates is the same fail-safe as two disagreeing
    remotes: take no action rather than query a coin-flip repo.
    """
    assert prg.repo_context(
        workspace_path=str(tmp_path / "missing"),
        body="see src/utils then repo ANG-Ventures/hermes-agent merge #5",
    ) is None


def test_repo_context_prefers_the_corroborated_slug(tmp_path: Path) -> None:
    """A slug also seen in a PR URL / qualified ref wins over a bare one."""
    assert prg.repo_context(
        workspace_path=str(tmp_path / "missing"),
        body=(
            "see src/utils then merge "
            "https://github.com/ANG-Ventures/hermes-agent/pull/808 "
            "and also #809"
        ),
    ) == "ANG-Ventures/hermes-agent"
    assert prg.repo_context(
        workspace_path=str(tmp_path / "missing"),
        body="see src/utils then merge ANG-Ventures/hermes-agent#808 and #809",
    ) == "ANG-Ventures/hermes-agent"


def test_repo_context_is_none_when_remotes_disagree_and_body_is_silent(
    tmp_path: Path,
) -> None:
    """Fail-safe: never GUESS which of two remotes a bare ``#N`` means.

    The hermes-agent checkout has ``origin`` = NousResearch and ``fork`` =
    ANG-Ventures, so a bare ``#787`` is genuinely ambiguous. Returning None
    makes the re-evaluator take no action rather than unblock on a coin flip.
    """
    repo = tmp_path / "wt"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(
        ["git", "remote", "add", "origin", "git@github.com:a/one.git"],
        cwd=repo, check=True,
    )
    subprocess.run(
        ["git", "remote", "add", "fork", "git@github.com:b/two.git"],
        cwd=repo, check=True,
    )
    assert prg.repo_context(workspace_path=str(repo), body=None) is None
    # ... but an explicit body mention disambiguates it.
    assert prg.repo_context(
        workspace_path=str(repo), body="PR is on b/two"
    ) == "b/two"


# ---------------------------------------------------------------------------
# State machine
# ---------------------------------------------------------------------------


def _blocked_card(conn, *, reason: str, kind: str = "needs_input", body=None):
    tid = kb.create_task(conn, title="gated card", body=body)
    kb.claim_task(conn, tid)
    assert kb.block_task(
        conn, tid, reason=reason, kind=kind,
        expected_run_id=kb.get_task(conn, tid).current_run_id,
    )
    return tid


def test_all_merged_unblocks_and_records_one_event(kanban_home: Path) -> None:
    with kanban_db_connect.connect() as conn:
        tid = _blocked_card(
            conn,
            reason="merge o/r#7 and o/r#8 then unblock me",
        )
        assert kb.get_task(conn, tid).status == "blocked"

        outcomes = prg.reevaluate_pr_gates(
            conn,
            query_fn=_stub({("o/r", 7): _merged("1111111122222222"),
                            ("o/r", 8): _merged("3333333344444444")}),
        )

    assert [o.action for o in outcomes] == ["unblocked"]
    with kanban_db_connect.connect() as conn:
        assert kb.get_task(conn, tid).status == "ready"
        events = [
            r["kind"] for r in conn.execute(
                "SELECT kind FROM task_events WHERE task_id = ?", (tid,)
            )
        ]
        assert events.count("gate_auto_resolved") == 1
        payload = json.loads(conn.execute(
            "SELECT payload FROM task_events "
            "WHERE task_id = ? AND kind = 'gate_auto_resolved'",
            (tid,),
        ).fetchone()["payload"])
        assert payload["prs"] == ["o/r#7", "o/r#8"]
        comments = [
            r["body"] for r in conn.execute(
                "SELECT body FROM task_comments WHERE task_id = ?", (tid,)
            )
        ]
        assert len(comments) == 1
        assert "gate satisfied" in comments[0]
        assert "o/r#7 merged 11111111" in comments[0]
        # The unblock reason and the comment are the same sentence.
        assert comments[0] == outcomes[0].detail


def test_one_open_pr_holds_the_card(kanban_home: Path) -> None:
    with kanban_db_connect.connect() as conn:
        tid = _blocked_card(conn, reason="merge o/r#7 and o/r#8 then unblock me")
        outcomes = prg.reevaluate_pr_gates(
            conn,
            query_fn=_stub({("o/r", 7): _merged(), ("o/r", 8): _open_pr()}),
        )
        assert [o.action for o in outcomes] == ["held"]
        assert kb.get_task(conn, tid).status == "blocked"
        assert conn.execute(
            "SELECT COUNT(*) c FROM task_comments WHERE task_id = ?", (tid,)
        ).fetchone()["c"] == 0


def test_closed_unmerged_comments_but_never_unblocks(kanban_home: Path) -> None:
    with kanban_db_connect.connect() as conn:
        tid = _blocked_card(conn, reason="merge o/r#9 then unblock me")
        outcomes = prg.reevaluate_pr_gates(
            conn, query_fn=_stub({("o/r", 9): _closed()}),
        )
        assert [o.action for o in outcomes] == ["closed_unmerged"]
        assert kb.get_task(conn, tid).status == "blocked"
        body = conn.execute(
            "SELECT body FROM task_comments WHERE task_id = ?", (tid,)
        ).fetchone()["body"]
        assert "closed without merge" in body
        assert "needs a human" in body
        kinds = [
            r["kind"] for r in conn.execute(
                "SELECT kind FROM task_events WHERE task_id = ?", (tid,)
            )
        ]
        assert "gate_auto_resolved" not in kinds


def test_closed_unmerged_comments_only_once_across_ticks(kanban_home: Path) -> None:
    """The advisory comment must not be re-posted every 60-second tick."""
    with kanban_db_connect.connect() as conn:
        tid = _blocked_card(conn, reason="merge o/r#9 then unblock me")
        for _ in range(3):
            prg.reevaluate_pr_gates(
                conn, query_fn=_stub({("o/r", 9): _closed()}),
            )
        assert conn.execute(
            "SELECT COUNT(*) c FROM task_comments WHERE task_id = ?", (tid,)
        ).fetchone()["c"] == 1


def test_closed_unmerged_advisory_is_keyed_to_the_current_pr_set(
    kanban_home: Path,
) -> None:
    """Re-blocking on a different dead PR gets its own human advisory."""
    with kb.connect() as conn:
        tid = _blocked_card(conn, reason="merge o/r#9 then unblock me")
        prg.reevaluate_pr_gates(conn, query_fn=_stub({("o/r", 9): _closed()}))

        # Simulate the operator re-pointing the existing blocked gate. The
        # re-evaluator keys off the latest blocked event, not stale task prose.
        with kb.write_txn(conn, allow_nested=True):
            kb._append_event(
                conn,
                tid,
                "blocked",
                {"reason": "replacement gate is o/r#10", "kind": "needs_input"},
            )
        prg.reevaluate_pr_gates(conn, query_fn=_stub({("o/r", 10): _closed()}))

        comments = [
            row["body"]
            for row in conn.execute(
                "SELECT body FROM task_comments WHERE task_id = ? ORDER BY id", (tid,)
            )
        ]
    assert len(comments) == 2
    assert "o/r#9" in comments[0]
    assert "o/r#10" in comments[1]


def test_lookup_failure_is_a_noop(kanban_home: Path) -> None:
    with kanban_db_connect.connect() as conn:
        tid = _blocked_card(conn, reason="merge o/r#7 then unblock me")
        outcomes = prg.reevaluate_pr_gates(conn, query_fn=_stub({}))
        assert [o.action for o in outcomes] == ["lookup_failed"]
        assert kb.get_task(conn, tid).status == "blocked"
        assert conn.execute(
            "SELECT COUNT(*) c FROM task_comments WHERE task_id = ?", (tid,)
        ).fetchone()["c"] == 0


def test_lookup_failure_is_deduplicated_within_one_tick(
    kanban_home: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    """A shared failed lookup costs one call and one warning opportunity per tick."""
    calls: list = []
    with kb.connect() as conn:
        for _ in range(3):
            _blocked_card(conn, reason="merge o/r#7 then unblock me")
        outcomes = prg.reevaluate_pr_gates(
            conn, query_fn=_stub({}, calls=calls),
        )
    assert calls == [("o/r", 7)]
    assert [o.action for o in outcomes] == ["lookup_failed"] * 3
    warnings = [
        record for record in caplog.records
        if "could not resolve PR state" in record.getMessage()
    ]
    assert len(warnings) == 1


def test_lookup_failure_is_not_cached(kanban_home: Path) -> None:
    """A transient ``gh`` failure must not poison the card for 5 minutes."""
    with kanban_db_connect.connect() as conn:
        tid = _blocked_card(conn, reason="merge o/r#7 then unblock me")
        prg.reevaluate_pr_gates(conn, query_fn=_stub({}))
        prg.reevaluate_pr_gates(conn, query_fn=_stub({("o/r", 7): _merged()}))
        assert kb.get_task(conn, tid).status == "ready"


# ---------------------------------------------------------------------------
# Scope fencing
# ---------------------------------------------------------------------------


def test_block_with_no_pr_reference_is_never_touched(kanban_home: Path) -> None:
    with kanban_db_connect.connect() as conn:
        tid = _blocked_card(conn, reason="need Ace to pick the retention window")
        calls: list = []
        outcomes = prg.reevaluate_pr_gates(
            conn, query_fn=_stub({}, calls=calls),
        )
        assert outcomes == []
        assert calls == []          # zero GitHub lookups burned
        assert kb.get_task(conn, tid).status == "blocked"


def test_transient_block_kind_is_out_of_scope(kanban_home: Path) -> None:
    with kanban_db_connect.connect() as conn:
        tid = _blocked_card(
            conn, reason="merge o/r#7 then unblock me", kind="transient",
        )
        outcomes = prg.reevaluate_pr_gates(
            conn, query_fn=_stub({("o/r", 7): _merged()}),
        )
        assert outcomes == []
        assert kb.get_task(conn, tid).status == "blocked"


def test_capability_block_kind_is_in_scope(kanban_home: Path) -> None:
    with kanban_db_connect.connect() as conn:
        tid = _blocked_card(
            conn, reason="merge o/r#7 then unblock me", kind="capability",
        )
        prg.reevaluate_pr_gates(conn, query_fn=_stub({("o/r", 7): _merged()}))
        assert kb.get_task(conn, tid).status == "ready"


def test_already_unblocked_card_is_not_reconsidered(kanban_home: Path) -> None:
    """The gate reads the LAST blocked event, and only while still blocked."""
    with kanban_db_connect.connect() as conn:
        tid = _blocked_card(conn, reason="merge o/r#7 then unblock me")
        assert kb.unblock_task(conn, tid)
        outcomes = prg.reevaluate_pr_gates(
            conn, query_fn=_stub({("o/r", 7): _merged()}),
        )
        assert outcomes == []
        events = [
            r["kind"] for r in conn.execute(
                "SELECT kind FROM task_events WHERE task_id = ?", (tid,)
            )
        ]
        assert "gate_auto_resolved" not in events


def test_bare_number_without_repo_context_is_left_alone(kanban_home: Path) -> None:
    with kanban_db_connect.connect() as conn:
        tid = _blocked_card(conn, reason="merge PR #787 then unblock me")
        calls: list = []
        outcomes = prg.reevaluate_pr_gates(
            conn, query_fn=_stub({}, calls=calls),
        )
        assert outcomes == []
        assert calls == []
        assert kb.get_task(conn, tid).status == "blocked"


def test_bare_number_resolves_against_the_card_body(kanban_home: Path) -> None:
    with kanban_db_connect.connect() as conn:
        tid = _blocked_card(
            conn,
            reason="merge PR #787 then unblock me",
            body="work lands on ANG-Ventures/hermes-agent",
        )
        prg.reevaluate_pr_gates(
            conn,
            query_fn=_stub({("ANG-Ventures/hermes-agent", 787): _merged()}),
        )
        assert kb.get_task(conn, tid).status == "ready"


# ---------------------------------------------------------------------------
# Cap + cache
# ---------------------------------------------------------------------------


def test_lookup_cap_is_enforced_per_tick(kanban_home: Path) -> None:
    calls: list = []
    with kanban_db_connect.connect() as conn:
        for n in range(5):
            _blocked_card(conn, reason=f"merge o/r#{100 + n} then unblock me")
        outcomes = prg.reevaluate_pr_gates(
            conn,
            query_fn=_stub({}, calls=calls),
            max_lookups=2,
        )
    assert len(calls) == 2
    # Cards beyond the budget are silently deferred to the next tick, not
    # reported as a failure.
    assert sum(1 for o in outcomes if o.action == "budget_exhausted") == 3


def test_unique_pr_is_queried_once_per_tick(kanban_home: Path) -> None:
    calls: list = []
    with kanban_db_connect.connect() as conn:
        for _ in range(3):
            _blocked_card(conn, reason="merge o/r#7 then unblock me")
        prg.reevaluate_pr_gates(
            conn,
            query_fn=_stub({("o/r", 7): _merged()}, calls=calls),
        )
        assert calls == [("o/r", 7)]
        assert all(
            r["status"] == "ready"
            for r in conn.execute("SELECT status FROM tasks")
        )


def test_open_state_is_cached_for_the_ttl_then_requeried(kanban_home: Path) -> None:
    calls: list = []
    clock = {"now": 1_000_000.0}
    with kanban_db_connect.connect() as conn:
        _blocked_card(conn, reason="merge o/r#7 then unblock me")
        qf = _stub({("o/r", 7): _open_pr()}, calls=calls)
        prg.reevaluate_pr_gates(conn, query_fn=qf, now=clock["now"])
        prg.reevaluate_pr_gates(conn, query_fn=qf, now=clock["now"] + 60)
        assert len(calls) == 1, "second tick inside the TTL must reuse the cache"
        prg.reevaluate_pr_gates(
            conn, query_fn=qf, now=clock["now"] + prg.CACHE_TTL_SECONDS + 1,
        )
        assert len(calls) == 2, "TTL expiry must re-query"


def test_closed_state_is_cached_for_ttl_then_requeried_after_reopen(
    kanban_home: Path,
) -> None:
    """CLOSED is reversible on GitHub; only MERGED may be cached forever."""
    calls: list = []
    responses = iter([_closed(), _merged()])

    def query(repo: str, number: int):
        calls.append((repo, number))
        return next(responses)

    with kb.connect() as conn:
        tid = _blocked_card(conn, reason="merge o/r#7 then unblock me")
        prg.reevaluate_pr_gates(conn, query_fn=query, now=1_000_000.0)
        task = kb.get_task(conn, tid)
        assert task is not None
        assert task.status == "blocked"
        prg.reevaluate_pr_gates(conn, query_fn=query, now=1_000_060.0)
        assert len(calls) == 1
        prg.reevaluate_pr_gates(
            conn,
            query_fn=query,
            now=1_000_000.0 + prg.CACHE_TTL_SECONDS + 1,
        )
        task = kb.get_task(conn, tid)
        assert task is not None
        assert task.status == "ready"
    assert calls == [("o/r", 7), ("o/r", 7)]


def test_merged_state_is_cached_permanently(kanban_home: Path) -> None:
    calls: list = []
    with kanban_db_connect.connect() as conn:
        _blocked_card(conn, reason="merge o/r#7 then unblock me")
        qf = _stub({("o/r", 7): _merged()}, calls=calls)
        prg.reevaluate_pr_gates(conn, query_fn=qf, now=1_000_000.0)
        _blocked_card(conn, reason="also merge o/r#7 then unblock me")
        prg.reevaluate_pr_gates(conn, query_fn=qf, now=1_000_000.0 + 86_400)
    assert len(calls) == 1, "MERGED is irreversible — never re-query it"


# ---------------------------------------------------------------------------
# Integration: the real dispatcher tick, with gh stubbed at the subprocess seam
# ---------------------------------------------------------------------------


def test_dispatch_tick_resolves_a_satisfied_gate(
    kanban_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end: one ``dispatch_once`` tick unblocks the card and reports it."""
    calls: list = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        assert argv[:2] == ["gh", "api"]
        assert argv[2] == "repos/o/r/pulls/7"
        return subprocess.CompletedProcess(
            argv, 0,
            stdout=json.dumps({
                "state": "closed",
                "merged_at": "2026-09-20T13:05:00Z",
                "merge_commit_sha": "deadbeefcafebabe",
            }),
            stderr="",
        )

    monkeypatch.setattr(prg.subprocess, "run", fake_run)

    with kanban_db_connect.connect() as conn:
        tid = _blocked_card(
            conn, reason="merge https://github.com/o/r/pull/7 then unblock me",
        )

    with kanban_db_connect.connect() as conn:
        result = kanban_db_dispatch.dispatch_once(conn, spawn_fn=lambda *a, **k: None)

    assert tid in result.gate_auto_resolved
    assert len(calls) == 1
    with kanban_db_connect.connect() as conn:
        task = kb.get_task(conn, tid)
        # Unblocked, and now spawnable again (it was ready-phase when blocked).
        assert task.status in {"ready", "running"}
        assert conn.execute(
            "SELECT COUNT(*) c FROM task_events "
            "WHERE task_id = ? AND kind = 'gate_auto_resolved'",
            (tid,),
        ).fetchone()["c"] == 1


def test_dispatch_tick_queries_github_before_taking_dispatch_lock(
    kanban_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Slow GitHub I/O must never extend the board's single-writer lock hold."""
    lock_held = False
    calls: list = []

    @contextlib.contextmanager
    def tracked_lock(_db_path):
        nonlocal lock_held
        assert not lock_held
        lock_held = True
        try:
            yield True
        finally:
            lock_held = False

    def query(repo: str, number: int):
        assert not lock_held, "GitHub lookup ran under _dispatch_tick_lock"
        calls.append((repo, number))
        return _merged()

    monkeypatch.setattr(kanban_db_connect, "_dispatch_tick_lock", tracked_lock)
    monkeypatch.setattr(prg, "query_pr", query)

    with kanban_db_connect.connect() as conn:
        tid = _blocked_card(conn, reason="merge o/r#7 then unblock me")
        result = kanban_db_dispatch.dispatch_once(
            conn, spawn_fn=lambda *a, **k: None,
        )
        task = kb.get_task(conn, tid)
        assert task is not None
        assert task.status in {"ready", "running"}

    assert calls == [("o/r", 7)]
    assert result.gate_auto_resolved == [tid]


def test_dispatch_tick_leaves_a_non_pr_block_alone(
    kanban_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    def explode(*a, **k):  # pragma: no cover - must never be called
        raise AssertionError("no gh lookup should happen for a non-PR block")

    monkeypatch.setattr(prg.subprocess, "run", explode)

    with kanban_db_connect.connect() as conn:
        tid = _blocked_card(conn, reason="need Ace to choose the cap")

    with kanban_db_connect.connect() as conn:
        result = kanban_db_dispatch.dispatch_once(conn, spawn_fn=lambda *a, **k: None)

    assert result.gate_auto_resolved == []
    with kanban_db_connect.connect() as conn:
        assert kb.get_task(conn, tid).status == "blocked"


def test_raising_lookup_warns_once_through_prefetch_and_reevaluate(
    kanban_home: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    """A raising ``gh`` seam is one failed lookup, so it is exactly one WARN.

    The prefetch pass and the locked re-evaluation pass are two halves of one
    tick. Before this test each half logged its own warning for the same failed
    unique PR, so a single unreachable PR paged the log twice per tick — the
    card's contract is "lookup failure = no action + one WARN".
    """
    calls: list = []

    def boom(repo: str, number: int):
        calls.append((repo, number))
        raise RuntimeError("gh exploded")

    with kb.connect() as conn:
        tid = _blocked_card(conn, reason="merge o/r#7 then unblock me")
        caplog.clear()
        with caplog.at_level("WARNING"):
            prefetched = prg.prefetch_pr_gate_states(conn, query_fn=boom)
            outcomes = prg.reevaluate_pr_gates(
                conn, query_fn=boom, prefetched=prefetched,
            )

    assert calls == [("o/r", 7)]
    assert [o.action for o in outcomes] == ["lookup_failed"]
    with kb.connect() as conn:
        assert kb.get_task(conn, tid).status == "blocked"
    warnings = [
        record for record in caplog.records
        if record.levelname == "WARNING"
        and "kanban PR-gate" in record.getMessage()
    ]
    assert len(warnings) == 1, [r.getMessage() for r in warnings]
    assert "o/r#7" in warnings[0].getMessage()


def test_raising_lookup_without_prefetch_is_a_noop_with_one_warning(
    kanban_home: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    """Direct (no-prefetch) callers get the same single-warning no-op."""

    def boom(repo: str, number: int):
        raise RuntimeError("gh exploded")

    with kb.connect() as conn:
        tid = _blocked_card(conn, reason="merge o/r#7 then unblock me")
        caplog.clear()
        with caplog.at_level("WARNING"):
            outcomes = prg.reevaluate_pr_gates(conn, query_fn=boom)
        assert [o.action for o in outcomes] == ["lookup_failed"]
        assert kb.get_task(conn, tid).status == "blocked"

    warnings = [
        record for record in caplog.records
        if record.levelname == "WARNING"
        and "kanban PR-gate" in record.getMessage()
    ]
    assert len(warnings) == 1, [r.getMessage() for r in warnings]
    assert "RuntimeError" in warnings[0].getMessage()
