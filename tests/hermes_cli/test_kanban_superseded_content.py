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
# file_carries(entry, candidate, merge_base, head): every side is the file's bytes, None = absent.


def test_pr_content_on_main_is_carried():
    patch = "@@ -1,3 +1,3 @@\n keep\n-old line\n+new line\n tail\n"
    mb, head = b"keep\nold line\ntail\n", b"keep\nnew line\ntail\n"
    assert sc.file_carries(_entry(patch), b"keep\nnew line\ntail\n", mb, head)


def test_key_present_but_value_absent_is_not_carried():
    """The 09:47 shape: main has the `requires:` key, just not the PR's value."""
    patch = ("@@ -1,3 +1,3 @@\n ---\n"
             "-metadata: {hermes: {portability: cli, requires: [uv]}}\n"
             "+metadata: {hermes: {portability: cli, requires: [], requires_bins: [uv]}}\n ---\n")
    mb = b"---\nmetadata: {hermes: {portability: cli, requires: [uv]}}\n---\n"
    head = b"---\nmetadata: {hermes: {portability: cli, requires: [], requires_bins: [uv]}}\n---\n"
    assert not sc.file_carries(_entry(patch), mb, mb, head)


def test_later_edit_on_main_superset_is_carried():
    patch = "@@ -1,2 +1,3 @@\n a\n+added by the PR\n b\n"
    mb, head = b"a\nb\n", b"a\nadded by the PR\nb\n"
    assert sc.file_carries(_entry(patch), b"a\nadded by the PR\nnewer line from another PR\nb\n", mb, head)


def test_removed_file_must_be_absent():
    assert sc.file_carries(_entry("", status="removed"), None, b"x\n", None)
    assert not sc.file_carries(_entry("", status="removed"), b"still here\n", b"still here\n", None)


def test_patchless_file_needs_the_exact_blob():
    data = b"\x00\x01binary"
    entry = {"filename": "a.bin", "status": "modified", "sha": sc._blob_sha(data)}
    assert sc.file_carries(entry, data, b"old", data)
    assert not sc.file_carries(entry, data + b"!", b"old", data)


def test_added_line_counted_as_a_multiset():
    """Prism 683cd59569ec: a line the PR adds twice must appear twice more than at merge-base."""
    patch = "@@ -1,2 +1,4 @@\n a\n+dup\n b\n+dup\n"
    mb, head = b"a\nb\n", b"a\ndup\nb\ndup\n"
    assert not sc.file_carries(_entry(patch), b"a\ndup\nb\n", mb, head)
    assert sc.file_carries(_entry(patch), b"a\ndup\nb\ndup\n", mb, head)
    # a line already at merge-base once, added once more by the PR: once on the candidate is not enough
    mb2, head2 = b"dup\na\n", b"dup\na\ndup\n"
    assert not sc.file_carries(_entry("@@ -1,2 +1,3 @@\n dup\n a\n+dup\n"), b"dup\na\n", mb2, head2)


def test_removed_lines_must_be_gone():
    """Prism 320e231efa19: a deletion-only PR is not vacuously carried while main keeps the lines."""
    patch = "@@ -1,3 +1,2 @@\n a\n-secret: leak\n b\n"
    mb, head = b"a\nsecret: leak\nb\n", b"a\nb\n"
    assert not sc.file_carries(_entry(patch), mb, mb, head)
    assert sc.file_carries(_entry(patch), b"a\nb\nnewer\n", mb, head)


# --- frontmatter: parsed key paths vs merge-base (the drain re-serializes headers) ---

BASE = b'---\nname: s\nmetadata: {hermes: {tags: [a], portability: portable, requires: [uv]}}\n---\n\n# s\n'
HEAD = b'---\nname: s\nmetadata: {hermes: {tags: [a], portability: cli, requires: [uv]}}\n---\n\n# s\n'
RETAG = ("@@ -1,3 +1,3 @@\n ---\n name: s\n"
         "-metadata: {hermes: {tags: [a], portability: portable, requires: [uv]}}\n"
         "+metadata: {hermes: {tags: [a], portability: cli, requires: [uv]}}\n ---\n")


def test_reserialized_frontmatter_with_the_pr_value_is_carried():
    main = (b"---\nname: s\nmetadata:\n  hermes:\n    tags: [a, b]\n    portability: cli\n"
            b"    requires: []\n---\n\n# s\n")  # tags/requires: newer edits the PR did not touch
    assert sc.file_carries(_entry(RETAG), main, BASE, HEAD)


def test_reserialized_frontmatter_without_the_pr_value_is_not_carried():
    main = b"---\nname: s\nmetadata:\n  hermes:\n    tags: [a]\n    portability: portable\n---\n\n# s\n"
    assert not sc.file_carries(_entry(RETAG), main, BASE, HEAD)


def test_frontmatter_body_lines_still_checked():
    """Prism 4e78214bba60: the body after the header goes through the line check."""
    patch = RETAG + "@@ -6,1 +6,2 @@\n # s\n+body line from the PR\n"
    main = b"---\nname: s\nmetadata:\n  hermes:\n    tags: [a]\n    portability: cli\n---\n\n# s\n"
    head = HEAD + b"body line from the PR\n"
    assert not sc.file_carries(_entry(patch), main, BASE, head)
    assert sc.file_carries(_entry(patch), main + b"body line from the PR\n", BASE, head)


def test_frontmatter_body_line_equal_to_a_header_line_still_checked():
    """Prism 4e78214bba60: a body line that also appears in the header is not exempted."""
    patch = "@@ -3,1 +3,2 @@\n ---\n+name: s\n"
    mb = b"---\nname: s\n---\n"
    head = b"---\nname: s\n---\nname: s\n"
    assert not sc.file_carries(_entry(patch), b"---\nname: s\n---\n", mb, head)
    assert sc.file_carries(_entry(patch), b"---\nname: s\n---\nname: s\n", mb, head)


def test_frontmatter_key_the_pr_removed_must_be_gone():
    head = b"---\nname: s\nmetadata: {hermes: {tags: [a], portability: portable}}\n---\n\n# s\n"
    patch = ("@@ -1,3 +1,3 @@\n ---\n name: s\n"
             "-metadata: {hermes: {tags: [a], portability: portable, requires: [uv]}}\n"
             "+metadata: {hermes: {tags: [a], portability: portable}}\n ---\n")
    kept = b"---\nname: s\nmetadata:\n  hermes:\n    tags: [a]\n    portability: portable\n    requires: [uv]\n---\n"
    gone = b"---\nname: s\nmetadata:\n  hermes:\n    tags: [a]\n    portability: portable\n---\n"
    assert not sc.file_carries(_entry(patch), kept, BASE, head)
    assert sc.file_carries(_entry(patch), gone, BASE, head)


def test_frontmatter_baseline_is_the_merge_base_not_base_sha():
    """Prism 04e61c328b13: the PR's change is measured from the merge-base. Here main (base.sha)
    already moved portability to `cli` after the PR branched; the PR set it to `cli` too, and the
    candidate has `portable`. Measured from base.sha the PR changed nothing and passes vacuously."""
    mb = b"---\nname: s\nportability: portable\n---\n"
    head = b"---\nname: s\nportability: cli\n---\n"
    patch = "@@ -1,4 +1,4 @@\n ---\n name: s\n-portability: portable\n+portability: cli\n ---\n"
    assert not sc.file_carries(_entry(patch), mb, mb, head)


def test_frontmatter_empty_mapping_is_a_value():
    """Prism d20063658cfe: `requires: {}` replacing a populated mapping must be carried as `{}`."""
    mb = b"---\nname: s\nrequires: {bins: [uv]}\n---\n"
    head = b"---\nname: s\nrequires: {}\n---\n"
    patch = "@@ -1,4 +1,4 @@\n ---\n name: s\n-requires: {bins: [uv]}\n+requires: {}\n ---\n"
    assert not sc.file_carries(_entry(patch), b"---\nname: s\n---\n", mb, head)
    assert not sc.file_carries(_entry(patch), b"---\nname: s\nrequires: {env: [X]}\n---\n", mb, head)
    assert sc.file_carries(_entry(patch), b"---\nname: s\nrequires:\n  {}\n---\n", mb, head)
    # an empty list is likewise a value, not a missing key
    mb2, head2 = b"---\nreq: [uv]\n---\n", b"---\nreq: []\n---\n"
    assert not sc.file_carries(_entry("@@ -1 +1 @@\n-req: [uv]\n+req: []\n"), b"---\n---\n", mb2, head2)


def test_frontmatter_value_must_be_deep_equal():
    """Prism f1da33336140 / 397de598ef77: the PR's value is compared parsed and whole, so a candidate
    that merely contains the key (or a list that only overlaps) is refused."""
    mb = b"---\nrequires_bins: [git]\n---\n"
    head = b"---\nrequires_bins: [git, uv]\n---\n"
    patch = "@@ -1,3 +1,3 @@\n ---\n-requires_bins: [git]\n+requires_bins: [git, uv]\n ---\n"
    assert not sc.file_carries(_entry(patch), b"---\nrequires_bins: [git]\n---\n", mb, head)
    assert not sc.file_carries(_entry(patch), b"---\nrequires_bins: [uv]\n---\n", mb, head)
    assert not sc.file_carries(_entry(patch), b"---\nrequires_bins: 'git, uv'\n---\n", mb, head)
    assert sc.file_carries(_entry(patch), b"---\nrequires_bins:\n  - git\n  - uv\n---\n", mb, head)


def test_frontmatter_unparseable_on_any_side_refuses():
    head = b"---\nname: s\nportability: cli\n---\n"
    mb = b"---\nname: s\nportability: portable\n---\n"
    patch = "@@ -1,4 +1,4 @@\n ---\n name: s\n-portability: portable\n+portability: cli\n ---\n"
    broken = b"---\nname: s\nportability: cli\n  bad: [\n---\n"
    unclosed = b"---\nname: s\nportability: cli\n"
    assert sc.file_carries(_entry(patch), head, mb, head)
    assert not sc.file_carries(_entry(patch), broken, mb, head)
    assert not sc.file_carries(_entry(patch), head, broken, head)
    assert not sc.file_carries(_entry(patch), head, mb, broken + b"x\n")
    assert not sc.file_carries(_entry(patch), unclosed, mb, head)


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


# --- pr_content_check(): the sha it returns must itself carry every file (Prism 6265717768a4) ---

OLD, TIP, BASE_SHA, HEAD_SHA, MB_SHA, OFF = "a" * 40, "b" * 40, "c" * 40, "d" * 40, "e" * 40, "f" * 40
ADDED = {"filename": "new.py", "status": "added", "patch": "@@ -0,0 +1,1 @@\n+print('landed')\n"}
OTHER = {"filename": "other.py", "status": "added", "patch": "@@ -0,0 +1,1 @@\n+print('other')\n"}


def _fake_github(monkeypatch, files, tree, on_default=(OLD, TIP)):
    """``tree[ref][path]`` = bytes at that ref; ``main`` resolves to TIP; MB is the merge-base."""
    import json as _json_mod
    shas = {OLD: OLD, TIP: TIP, "main": TIP, OFF: OFF}

    def fake_json(args):
        path = args[0]
        if path == f"repos/{REPO}/compare/{BASE_SHA}...{HEAD_SHA}":
            return {"merge_base_commit": {"sha": MB_SHA}}
        if path.startswith(f"repos/{REPO}/compare/{TIP}..."):
            return {"status": "behind" if path.rsplit("...", 1)[1] in on_default else "diverged"}
        if path.startswith(f"repos/{REPO}/commits/"):
            return {"sha": shas[path.rsplit("/", 1)[1]]}
        if path == f"repos/{REPO}":
            return {"default_branch": "main"}
        if path.startswith(f"repos/{REPO}/pulls/"):
            return {"base": {"sha": BASE_SHA}, "head": {"sha": HEAD_SHA}}
        raise AssertionError(path)

    monkeypatch.setattr(sc, "_json", fake_json)
    monkeypatch.setattr(sc, "_gh", lambda args, raw=False: "\n".join(_json_mod.dumps(f) for f in files))
    monkeypatch.setattr(sc, "_file_bytes", lambda repo, path, ref: tree.get(shas.get(ref, ref), {}).get(path))


HEAD_TREE = {"new.py": b"print('landed')\n", "other.py": b"print('other')\n"}


def test_content_only_on_tip_records_the_tip_not_the_requested_sha(monkeypatch):
    """Repro: main is at TIP with the PR's file; the pointer names an older OLD without it."""
    _fake_github(monkeypatch, [ADDED], {OLD: {}, TIP: {"new.py": b"print('landed')\n"}, HEAD_SHA: HEAD_TREE})
    out = sc.pr_content_check(REPO, 7, OLD)
    assert out["differs"] == []
    assert out["sha"] == TIP, "the survivor must be the commit that carries the content"
    assert out["requested"] == OLD
    survivors = sc.verify("t_x", op.extract_pr_refs(f"https://github.com/{REPO}/pull/7"), [OLD],
                          check=sc.pr_content_check)
    assert survivors[0]["sha"] == TIP


def test_requested_sha_wins_when_it_carries_everything(monkeypatch):
    _fake_github(monkeypatch, [ADDED], {OLD: {"new.py": b"print('landed')\n"},
                                        TIP: {"new.py": b"print('landed')\n"}, HEAD_SHA: HEAD_TREE})
    assert sc.pr_content_check(REPO, 7, OLD)["sha"] == OLD


def test_files_split_across_two_commits_is_not_carried(monkeypatch):
    """No single commit holds both files: nothing may be promoted to survivor evidence."""
    _fake_github(monkeypatch, [ADDED, OTHER], {OLD: {"new.py": b"print('landed')\n"},
                                               TIP: {"other.py": b"print('other')\n"}, HEAD_SHA: HEAD_TREE})
    out = sc.pr_content_check(REPO, 7, OLD)
    assert out["differs"] in (["new.py"], ["other.py"])
    with pytest.raises(sc.SupersededContentError):
        sc.verify("t_x", op.extract_pr_refs(f"https://github.com/{REPO}/pull/7"), [OLD],
                  check=sc.pr_content_check)


def test_sha_off_the_default_branch_is_refused_before_content(monkeypatch):
    """Prism 4aa42857aa36: a branch-only commit carrying the content is not a superseder, and is
    never recorded as on-default survivor evidence, even though the tip carries the content too."""
    tree = {OFF: {"new.py": b"print('landed')\n"}, TIP: {"new.py": b"print('landed')\n"}, HEAD_SHA: HEAD_TREE}
    _fake_github(monkeypatch, [ADDED], tree)
    out = sc.pr_content_check(REPO, 7, OFF)
    assert out["off_default"] is True and not out["sha"]
    with pytest.raises(sc.SupersededContentError) as err:
        sc.verify("t_x", op.extract_pr_refs(f"https://github.com/{REPO}/pull/7"), [OFF],
                  check=sc.pr_content_check)
    assert err.value.off_default == [OFF]
    assert "not on the default branch" in str(err.value)


def test_merge_base_not_base_sha_is_read(monkeypatch):
    """Prism 04e61c328b13 at the fetch layer: the PR-side baseline bytes come from the merge-base."""
    seen = []
    _fake_github(monkeypatch, [{"filename": "x.md", "status": "modified",
                                "patch": "@@ -1 +1 @@\n-old\n+new\n"}],
                 {MB_SHA: {"x.md": b"old\n"}, HEAD_SHA: {"x.md": b"new\n"}, TIP: {"x.md": b"new\n"},
                  OLD: {"x.md": b"new\n"}, BASE_SHA: {"x.md": b"new\n"}})
    real = sc._file_bytes
    monkeypatch.setattr(sc, "_file_bytes", lambda repo, path, ref: (seen.append(ref), real(repo, path, ref))[1])
    assert sc.pr_content_check(REPO, 7, OLD)["differs"] == []
    assert MB_SHA in seen and BASE_SHA not in seen


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
    from hermes_cli import kanban_superseded_open as so
    monkeypatch.setattr(so, "pr_thread", lambda repo, n: None)   # t_ec8ceae2: no close-record read here


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


# --- round 3 (Apollo send-back @ 1ff9ac0a) ------------------------------------


def test_summary_only_sha_token_is_content_checked(board, monkeypatch):
    """Prism abd009e5ca09 / 37e57666a117: `SUPERSEDED-BY <sha>` in --summary (no --superseded-by flag)
    is the path t_c889cc2d was closed on. It must go through the content gate too."""
    _gate_stubs(monkeypatch, _check(
        **{SHA: {"sha": SHA, "default": "main", "differs": ["skills-shared/smart-home/cad-part/SKILL.md"]}}))
    with kb.connect() as conn:
        tid, run = _drain_card(conn)
        with pytest.raises(sc.SupersededContentError, match="cad-part/SKILL.md"):
            kb.complete_task(conn, tid, expected_run_id=run, summary=f"carve landed SUPERSEDED-BY {SHA}",
                             metadata={"pr_url": PR_URL})
        with pytest.raises(sc.SupersededContentError):
            kb.complete_task(conn, tid, expected_run_id=run, result=f"#4736 RE-CARRIED-AS {SHA[:12]}",
                             metadata={"pr_url": PR_URL})
        assert conn.execute("SELECT status FROM tasks WHERE id=?", (tid,)).fetchone()[0] == "running"


def test_summary_only_sha_token_carrying_the_content_closes(board, monkeypatch):
    _gate_stubs(monkeypatch, _check(**{SHA: {"sha": SHA, "default": "main", "differs": []}}))
    with kb.connect() as conn:
        tid, run = _drain_card(conn)
        assert kb.complete_task(conn, tid, expected_run_id=run, summary=f"SUPERSEDED-BY {SHA}",
                                metadata={"pr_url": PR_URL}) is True
        assert kb.list_runs(conn, tid)[-1].metadata["survivor"]["refs"][0]["sha"] == SHA


def test_token_shas_skip_pr_targets():
    assert sc.token_shas(f"x SUPERSEDED-BY {SHA[:11]}; y RE-CARRIED-AS #9", None,
                         "SUPERSEDED-BY ANG-Ventures/hermes-home#4736") == [SHA[:11]]


def test_rename_is_checked_at_the_new_path_and_old_path_gone(monkeypatch):
    """Prism 1b9ec408800f: a rename is carried only when the new path holds the content AND the old
    path is gone; a copy beside the original is not the PR's rename."""
    entry = {"filename": "new/x.md", "previous_filename": "old/x.md", "status": "renamed",
             "patch": "@@ -1 +1 @@\n-old body\n+new body\n"}
    tree = {MB_SHA: {"old/x.md": b"old body\n"}, HEAD_SHA: {"new/x.md": b"new body\n"},
            TIP: {"new/x.md": b"new body\n"},
            OLD: {"new/x.md": b"new body\n", "old/x.md": b"old body\n"}}
    _fake_github(monkeypatch, [entry], tree)
    assert sc.pr_content_check(REPO, 7, OLD)["sha"] == TIP  # OLD still has the old path
    tree[TIP]["old/x.md"] = b"old body\n"
    assert sc.pr_content_check(REPO, 7, OLD)["differs"] == ["new/x.md"]


def test_pure_rename_without_patch_still_needs_old_path_gone(monkeypatch):
    entry = {"filename": "new/x.md", "previous_filename": "old/x.md", "status": "renamed",
             "sha": sc._blob_sha(b"same\n")}
    tree = {MB_SHA: {"old/x.md": b"same\n"}, HEAD_SHA: {"new/x.md": b"same\n"},
            TIP: {"new/x.md": b"same\n", "old/x.md": b"same\n"}, OLD: {"new/x.md": b"same\n"}}
    _fake_github(monkeypatch, [entry], tree)
    out = sc.pr_content_check(REPO, 7, OLD)
    assert out["sha"] == OLD and out["differs"] == []
    tree[OLD]["old/x.md"] = b"same\n"
    assert sc.pr_content_check(REPO, 7, OLD)["differs"] == ["new/x.md"]


def test_frontmatter_equality_is_typed_at_every_depth():
    """Prism 78093f0a74dc: `1` is not `true` and not `1.0`, also inside a list (Python's 1 == True)."""
    mb = b"---\nflags: [0]\nn: 0\n---\n"
    head = b"---\nflags: [1]\nn: 1\n---\n"
    patch = "@@ -1,4 +1,4 @@\n ---\n-flags: [0]\n-n: 0\n+flags: [1]\n+n: 1\n ---\n"
    assert sc.file_carries(_entry(patch), b"---\nflags:\n  - 1\nn: 1\n---\n", mb, head)
    assert not sc.file_carries(_entry(patch), b"---\nflags: [true]\nn: 1\n---\n", mb, head)
    assert not sc.file_carries(_entry(patch), b"---\nflags: [1]\nn: 1.0\n---\n", mb, head)
    assert not sc.file_carries(_entry(patch), b"---\nflags: [1]\nn: '1'\n---\n", mb, head)


def test_frontmatter_formatting_only_difference_is_carried():
    """Quoting / flow vs block / comments are formatting, not content."""
    mb = b"---\nname: s\nmeta: {a: x}\n---\n"
    head = b"---\nname: s\nmeta: {a: 'y', b: [1, 2]}\n---\n"
    patch = "@@ -1,4 +1,4 @@\n ---\n name: s\n-meta: {a: x}\n+meta: {a: 'y', b: [1, 2]}\n ---\n"
    cand = b'---\nname: s\nmeta:\n  # reflowed by the drain\n  a: "y"\n  b:\n    - 1\n    - 2\n---\n'
    assert sc.file_carries(_entry(patch), cand, mb, head)


def test_reorder_is_not_carried_by_the_multiset():
    """Prism b4f089dc2ff6: a PR whose whole point is reordering lines is not carried by the
    merge-base order, though every line count matches."""
    mb, head = b"a\nb\nc\n", b"c\na\nb\n"
    patch = "@@ -1,3 +1,3 @@\n+c\n a\n b\n-c\n"
    assert not sc.file_carries(_entry(patch), mb, mb, head)
    assert sc.file_carries(_entry(patch), head, mb, head)
    # a moved line inside a frontmatter body
    fm_mb = b"---\nname: s\n---\nstep one\nstep two\nstep three\n"
    fm_head = b"---\nname: s\n---\nstep two\nstep one\nstep three\n"
    assert not sc.file_carries(_entry("@@ -4,2 +4,2 @@\n+step two\n step one\n-step two\n"),
                               fm_mb, fm_mb, fm_head)


def test_order_check_tolerates_later_edits_around_the_block():
    """A newer edit on main that inserts lines between the PR's block and its neighbours, or edits
    elsewhere in the file, is still a superset."""
    mb, head = b"a\nb\nc\nd\n", b"a\nb\nNEW1\nNEW2\nc\nd\n"
    patch = "@@ -2,2 +2,4 @@\n b\n+NEW1\n+NEW2\n c\n"
    cand = b"top newer\na\nb\nNEW1\ninserted later\nNEW2\nc\nd changed later\nd\n"
    assert sc.file_carries(_entry(patch), cand, mb, head)
    assert not sc.file_carries(_entry(patch), b"a\nb\nNEW2\nNEW1\nc\nd\n", mb, head)


def test_archive_runs_the_content_gate_on_a_sha_superseder(board, monkeypatch):
    """Prism a3069816b211: an earlier run summary `SUPERSEDED-BY <sha>` whose sha is on main but lacks
    the PR's content must not let archive_task hide the card (complete_task already refuses it)."""
    _gate_stubs(monkeypatch, _check(
        **{SHA: {"sha": SHA, "default": "main", "differs": ["skills-shared/smart-home/cad-part/SKILL.md"]}}))
    with kb.connect() as conn:
        tid, run = _drain_card(conn)
        conn.execute("UPDATE task_runs SET summary=? WHERE id=?", (f"carve landed SUPERSEDED-BY {SHA}", run))
        conn.commit()
        with pytest.raises(sc.SupersededContentError, match="cad-part/SKILL.md"):
            kb.archive_task(conn, tid)
        assert conn.execute("SELECT status FROM tasks WHERE id=?", (tid,)).fetchone()[0] != "archived"


def test_archive_with_a_content_carrying_sha_still_archives(board, monkeypatch):
    _gate_stubs(monkeypatch, _check(**{SHA: {"sha": SHA, "default": "main", "differs": []}}))
    with kb.connect() as conn:
        tid, run = _drain_card(conn)
        conn.execute("UPDATE task_runs SET summary=? WHERE id=?", (f"SUPERSEDED-BY {SHA}", run))
        conn.commit()
        assert kb.archive_task(conn, tid) is True


# --- residual Prism P1s from #2058 (t_194cdb7e) ------------------------------


def test_insertion_in_another_scope_is_not_carried():
    """Prism 86fbd795f101: `run()` the PR added to `a()` is not carried by a `run()` in `b()`. The
    inserted block must sit right after its unchanged predecessor on the candidate."""
    mb = ["def a():", "    pass", "def b():", "    pass"]
    head = ["def a():", "    run()", "    pass", "def b():", "    pass"]
    wrong = ["def a():", "    pass", "def b():", "    run()", "    pass"]
    assert not sc.lines_carry(mb, head, wrong)
    assert sc.lines_carry(mb, head, head)
    # a newer edit elsewhere, or a line added later after the block, still carries it
    assert sc.lines_carry(mb, head, ["# newer", "def a():", "    run()", "    log()", "    pass",
                                     "def b():", "    pass"])


def test_frontmatter_deleted_key_replaced_by_a_mapping_is_not_carried():
    """Prism d3b02265e3bf: a scalar key the PR deleted is still present when the candidate holds a
    mapping under it; flattening to leaf paths hid it."""
    assert not sc.frontmatter_carries({"x": 1, "keep": 0}, {"keep": 0}, {"x": {"nested": 2}, "keep": 0})
    assert sc.frontmatter_carries({"x": 1, "keep": 0}, {"keep": 0}, {"keep": 0})
    # a deleted nested key the candidate still holds as a container is likewise not gone
    assert not sc.frontmatter_carries({"m": {"x": 1, "k": 0}}, {"m": {"k": 0}}, {"m": {"x": {"y": 1}, "k": 0}})
    assert not sc.frontmatter_carries({"m": {"x": 1, "k": 0}}, {"m": {"k": 0}}, {"m": {"x": [1], "k": 0}})
    # a scalar the PR turned into a mapping is carried by that mapping
    assert sc.frontmatter_carries({"x": 1}, {"x": {"n": 2}}, {"x": {"n": 2}})


# --- t_ec8ceae2 / t_87e32ff7: an equivalent fix worded differently --------------------------
# hermes-home#7135 closed `CLOSED: SUPERSEDED-BY 6a6fd050d93 -- main fixed the same three anchors` by a fleet bot;
# 6a6fd050d93 touched scripts/tests/mutate_prism_completed_required.sh with different comment lines, so the line match
# refused. The PR's own trusted close record naming the sha + that commit touching every differing file is the survivor.
F87 = "scripts/tests/mutate_prism_completed_required.sh"


def _sha_close(monkeypatch, *, login="ang-fleet-interactive[bot]", files=(F87,), named=SHA[:11]):
    from hermes_cli import kanban_superseded_open as so
    body = f"CLOSED: SUPERSEDED-BY {named} -- main fixed the same anchors independently [card t_87e32ff7]"
    monkeypatch.setattr(so, "pr_thread", lambda repo, n: {"body": "", "comments": [
        {"body": body, "login": login, "assoc": "NONE"}]})
    monkeypatch.setattr(so, "commit_files", lambda repo, sha: (SHA, set(files)) if SHA.startswith(sha) else None)


def test_e2e_close_record_sha_touching_every_differing_file_closes(board, monkeypatch):
    _gate_stubs(monkeypatch, _check(**{SHA: {"sha": SHA, "default": "main", "differs": [F87]}}))
    _sha_close(monkeypatch)
    with kb.connect() as conn:
        tid, run = _drain_card(conn)
        assert kb.complete_task(conn, tid, expected_run_id=run, superseded_by=SHA, summary="fixed on main") is True
        assert conn.execute("SELECT status FROM tasks WHERE id=?", (tid,)).fetchone()[0] == "done"
        ev = [e for e in kb.list_events(conn, tid) if e.kind == "routed_pr_superseded"]
        assert ev and ev[0].payload["close_record_sha"] == {f"{REPO}#4736": SHA}
        assert kb.list_runs(conn, tid)[-1].metadata["superseded_content"][0]["corroborated_by"] == \
            "close-record+file-coverage"


@pytest.mark.parametrize("variant", [
    {"files": ("some/other/file.py",)},              # the 4729 shape: the commit did not touch the differing file
    {"login": "random-user"},                        # an untrusted author's CLOSED: line
    {"named": "0123456789a"},                        # the record names a different sha
])
def test_e2e_close_record_mutants_still_refused(board, monkeypatch, variant):
    _gate_stubs(monkeypatch, _check(**{SHA: {"sha": SHA, "default": "main", "differs": [F87]}}))
    _sha_close(monkeypatch, **variant)
    with kb.connect() as conn:
        tid, run = _drain_card(conn)
        with pytest.raises(sc.SupersededContentError):
            kb.complete_task(conn, tid, expected_run_id=run, superseded_by=SHA, summary="x")
        assert conn.execute("SELECT status FROM tasks WHERE id=?", (tid,)).fetchone()[0] == "running"
