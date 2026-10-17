"""``--superseded-by <sha>`` is a verified claim about CONTENT, not ancestry (t_8035c8af).

hermes-home#4729 was closed ``SUPERSEDED-BY e4d8ee8797e3`` at 09:47 on 2026-10-17 because a
frontmatter KEY the PR touched existed on main, while the PR's new VALUE did not. And t_c889cc2d,
whose carve did land via the home-autocommit drain (4b3026560f3), took four tries to close because
the main sha "does not name the card". A passing content check is now the survivor evidence.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_open_pr as op
from hermes_cli import kanban_supersede_content as sc

REPO = "ANG-Ventures/hermes-home"
PR_URL = f"https://github.com/{REPO}/pull/4736"
SHA = "4b3026560f3d04a657c87b2d5cdffda87b1fd179"


def _entry(patch, status="modified", filename="skills/x/SKILL.md"):
    return {"filename": filename, "status": status, "patch": patch}


# --- line-level file check -------------------------------------------------


def test_pr_content_on_main_is_carried():
    patch = "@@ -1,3 +1,3 @@\n keep\n-old line\n+new line\n tail\n"
    assert sc.file_carries(_entry(patch), b"keep\nnew line\ntail\n")


def test_key_present_but_value_absent_is_not_carried():
    """The 09:47 shape: main has the `requires:` key, just not the PR's value."""
    patch = ("@@ -1,3 +1,3 @@\n ---\n"
             "-metadata: {hermes: {portability: cli, requires: [uv]}}\n"
             "+metadata: {hermes: {portability: cli, requires: [], requires_bins: [uv]}}\n ---\n")
    main = b"---\nmetadata: {hermes: {portability: cli, requires: [uv]}}\n---\n"
    assert not sc.file_carries(_entry(patch), main)


def test_later_edit_on_main_superset_is_carried():
    patch = "@@ -1,2 +1,3 @@\n a\n+added by the PR\n b\n"
    assert sc.file_carries(_entry(patch), b"a\nadded by the PR\nnewer line from another PR\nb\n")


def test_removed_file_must_be_absent():
    assert sc.file_carries(_entry("", status="removed"), None)
    assert not sc.file_carries(_entry("", status="removed"), b"still here\n")


def test_patchless_file_needs_the_exact_blob():
    data = b"\x00\x01binary"
    entry = {"filename": "a.bin", "status": "modified", "sha": sc._blob_sha(data)}
    assert sc.file_carries(entry, data)
    assert not sc.file_carries(entry, data + b"!")


# --- key-level frontmatter fallback (the drain re-serializes headers) ------

BASE = b'---\nname: s\nmetadata: {hermes: {tags: [a], portability: portable, requires: [uv]}}\n---\n\n# s\n'
HEAD = b'---\nname: s\nmetadata: {hermes: {tags: [a], portability: cli, requires: [uv]}}\n---\n\n# s\n'
RETAG = ("@@ -1,3 +1,3 @@\n ---\n name: s\n"
         "-metadata: {hermes: {tags: [a], portability: portable, requires: [uv]}}\n"
         "+metadata: {hermes: {tags: [a], portability: cli, requires: [uv]}}\n ---\n")


def test_reserialized_frontmatter_with_the_pr_value_is_carried():
    main = (b"---\nname: s\nmetadata:\n  hermes:\n    tags: [a, b]\n    portability: cli\n"
            b"    requires: []\n---\n\n# s\n")  # tags/requires: newer edits the PR did not touch
    assert not sc.file_carries(_entry(RETAG), main)
    assert sc.frontmatter_carries(_entry(RETAG), main, BASE, HEAD)


def test_reserialized_frontmatter_without_the_pr_value_is_not_carried():
    main = b"---\nname: s\nmetadata:\n  hermes:\n    tags: [a]\n    portability: portable\n---\n\n# s\n"
    assert not sc.frontmatter_carries(_entry(RETAG), main, BASE, HEAD)


def test_frontmatter_fallback_still_checks_body_lines():
    patch = RETAG + "@@ -6,1 +6,2 @@\n # s\n+body line from the PR\n"
    main = b"---\nname: s\nmetadata:\n  hermes:\n    tags: [a]\n    portability: cli\n---\n\n# s\n"
    assert not sc.frontmatter_carries(_entry(patch), main, BASE, HEAD + b"body line from the PR\n")
    assert sc.frontmatter_carries(_entry(patch), main + b"body line from the PR\n", BASE,
                                  HEAD + b"body line from the PR\n")


def test_frontmatter_key_the_pr_removed_must_be_gone():
    head = b"---\nname: s\nmetadata: {hermes: {tags: [a], portability: portable}}\n---\n\n# s\n"
    patch = ("@@ -1,3 +1,3 @@\n ---\n name: s\n"
             "-metadata: {hermes: {tags: [a], portability: portable, requires: [uv]}}\n"
             "+metadata: {hermes: {tags: [a], portability: portable}}\n ---\n")
    kept = b"---\nname: s\nmetadata:\n  hermes:\n    tags: [a]\n    portability: portable\n    requires: [uv]\n---\n"
    gone = b"---\nname: s\nmetadata:\n  hermes:\n    tags: [a]\n    portability: portable\n---\n"
    assert not sc.frontmatter_carries(_entry(patch), kept, BASE, head)
    assert sc.frontmatter_carries(_entry(patch), gone, BASE, head)


# --- verify(): the gate's verdict --------------------------------------------


def _check(**by_sha):
    return lambda repo, number, sha: by_sha.get(sha)


def test_verify_accepts_and_returns_the_survivor_ref():
    refs = op.extract_pr_refs(PR_URL)
    out = sc.verify("t_x", refs, [SHA], check=_check(**{SHA: {"sha": SHA, "default": "main", "differs": []}}))
    assert out and out[0]["sha"] == SHA and out[0]["pr"] == f"{REPO}#4736"
    assert out[0]["corroborated_by"] == "superseded-content"


def test_verify_refuses_naming_the_files():
    refs = op.extract_pr_refs(PR_URL)
    with pytest.raises(sc.SupersededContentError) as err:
        sc.verify("t_x", refs, [SHA], check=_check(
            **{SHA: {"sha": SHA, "default": "main", "differs": ["skills/cad-part/SKILL.md"]}}))
    assert "skills/cad-part/SKILL.md" in str(err.value)
    assert err.value.differs == {f"{REPO}#4736": ["skills/cad-part/SKILL.md"]}


def test_verify_unreadable_fails_closed():
    with pytest.raises(sc.SupersededContentError) as err:
        sc.verify("t_x", op.extract_pr_refs(PR_URL), [SHA], check=_check())
    assert err.value.unverified == [f"{REPO}#4736"]


def test_shas_are_parsed_from_the_pointer_not_card_ids_or_pr_numbers():
    assert sc.supersede_shas(f"t_c889cc2d -> {SHA[:11]} (#4736)") == [SHA[:11]]
    assert sc.supersede_shas("t_0c5ac29a -> #889") == []


# --- E2E through complete_task on a temp board ---------------------------


@pytest.fixture
def board(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HERMES_" + "HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _drain_card(conn):
    """A scratch card whose worker claimed changed files and named its (now closed) PR."""
    tid = kb.create_task(conn, title="carve", assignee="worker")
    kb.claim_task(conn, tid)
    run = conn.execute("SELECT current_run_id FROM tasks WHERE id=?", (tid,)).fetchone()[0]
    conn.execute("UPDATE task_runs SET metadata=? WHERE id=?",
                 ('{"pr_url": "%s", "changed_files": ["SKILL.md"]}' % PR_URL, run))
    conn.commit()
    return tid, run


def _gate_stubs(monkeypatch, content):
    """Every GitHub oracle stubbed. The closed-unmerged gate only consults its sha-ancestry oracle
    outside pytest, so drop the marker for this test and stub that oracle instead."""
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setattr(op, "_default_query", lambda: (lambda repo, n: {"state": "CLOSED"}))
    monkeypatch.setattr(op, "sha_on_default", lambda repo, sha: True)
    monkeypatch.setattr(op, "_default_close_record", lambda: None)
    monkeypatch.setattr(sc, "default_content_check", lambda: content)


def test_e2e_content_on_main_closes_without_a_survivor_ref(board, monkeypatch):
    _gate_stubs(monkeypatch, _check(**{SHA: {"sha": SHA, "default": "main", "differs": []}}))
    with kb.connect() as conn:
        tid, run = _drain_card(conn)
        assert kb.complete_task(conn, tid, expected_run_id=run, superseded_by=SHA,
                                summary="carve landed via the drain") is True
        assert conn.execute("SELECT status FROM tasks WHERE id=?", (tid,)).fetchone()[0] == "done"
        meta = kb.list_runs(conn, tid)[-1].metadata
        assert meta["survivor"]["kind"] == "ref"
        assert meta["survivor"]["refs"][0]["sha"] == SHA
        assert meta["superseded_content"][0]["pr"] == f"{REPO}#4736"


def test_e2e_content_absent_refused_naming_the_file(board, monkeypatch):
    _gate_stubs(monkeypatch, _check(
        **{SHA: {"sha": SHA, "default": "main", "differs": ["skills-shared/smart-home/cad-part/SKILL.md"]}}))
    with kb.connect() as conn:
        tid, run = _drain_card(conn)
        with pytest.raises(sc.SupersededContentError, match="cad-part/SKILL.md"):
            kb.complete_task(conn, tid, expected_run_id=run, superseded_by=SHA, summary="x")
        assert conn.execute("SELECT status FROM tasks WHERE id=?", (tid,)).fetchone()[0] == "running"
        kinds = [e.kind for e in kb.list_events(conn, tid)]
        assert "completion_blocked_superseded_content" in kinds
        assert "completed" not in kinds


def test_e2e_card_pointer_without_sha_is_not_content_checked(board, monkeypatch):
    def boom(*_a):
        raise AssertionError("no content lookup for a card/PR pointer")
    monkeypatch.setattr(sc, "default_content_check", lambda: boom)
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="already fixed", assignee="worker")
        kb.claim_task(conn, tid)
        assert kb.complete_task(conn, tid, superseded_by="t_0c5ac29a -> #889") is True
