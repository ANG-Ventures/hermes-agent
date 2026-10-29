"""A background curator remove_file must not lose a reference file's content.

The weekly curator fold condensed reference files into an umbrella and then remove_file'd the
originals, so every line the condensation dropped existed nowhere on disk and the source stub kept
linking the removed path. The remove is allowed only once every distinctive line is carried by
another skill file and the source SKILL.md no longer links it."""

import contextvars
import json
import os
import threading
from contextlib import contextmanager
from unittest.mock import patch

import pytest

import tools.skill_manager_guards as guards
from tools.skill_manager_guards import _reset_background_review_read_marks
from tools.skill_manager_tool import skill_manage
from tools.skills_tool import skill_view

REFERENCE = (
    "# FD plateau soak test\n\n"
    "Run the client against a fake server for 30 minutes at 50 requests per second.\n"
    "Sample lsof -p <pid> | wc -l every 10 seconds and write the series to soak.csv.\n"
    "A leak shows as a monotonic climb; a healthy pool plateaus near max_connections + 12.\n"
    "Kill the server mid-run once to prove reconnects release the dead sockets.\n"
)
CONDENSED = "# FD hygiene\n\nSoak the client and watch lsof; a healthy pool plateaus.\n"


def _skill_md(name: str, body: str = "") -> str:
    return f"---\nname: {name}\ndescription: Use when testing {name}.\n---\n\n# {name}\n\nStep one.\n{body}"


@contextmanager
def _curator(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    root = home / "skills"
    root.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    _reset_background_review_read_marks()
    with patch("tools.skill_manager_tool.SKILLS_DIR", root), \
         patch("tools.skill_manager_tool.HERMES_HOME", home), \
         patch("tools.skills_tool.SKILLS_DIR", root), \
         patch("agent.skill_utils.get_all_skills_dirs", return_value=[root]), \
         patch("tools.skill_usage._is_curator_managed_record", return_value=True), \
         patch("tools.skill_provenance.is_background_review", return_value=True):
        yield root
    _reset_background_review_read_marks()


def _plant(root, name, body="", refs=None):
    (root / name).mkdir()
    (root / name / "SKILL.md").write_text(_skill_md(name, body), encoding="utf-8")
    for rel, text in (refs or {}).items():
        (root / name / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / name / rel).write_text(text, encoding="utf-8")


def _remove(name, rel):
    assert json.loads(skill_view(name, rel))["success"] is True
    return json.loads(skill_manage(action="remove_file", name=name, file_path=rel))


def test_condensing_fold_refuses_the_remove(tmp_path, monkeypatch):
    with _curator(tmp_path, monkeypatch) as root:
        _plant(root, "source", refs={"references/fd-plateau-soak-test.md": REFERENCE})
        _plant(root, "umbrella", refs={"references/fd-hygiene.md": CONDENSED})
        result = _remove("source", "references/fd-plateau-soak-test.md")
    assert result["success"] is False, result
    assert result.get("_content_not_carried") is True
    assert result["uncarried_line_count"] >= 4
    assert "Move the file WHOLE" in result["error"]
    assert (root / "source" / "references" / "fd-plateau-soak-test.md").read_text() == REFERENCE


def test_byte_identical_move_passes(tmp_path, monkeypatch):
    with _curator(tmp_path, monkeypatch) as root:
        _plant(root, "source", refs={"references/fd-plateau-soak-test.md": REFERENCE})
        _plant(root, "umbrella", refs={"references/fd-plateau-soak-test.md": REFERENCE})
        result = _remove("source", "references/fd-plateau-soak-test.md")
    assert result["success"] is True, result
    assert not (root / "source" / "references" / "fd-plateau-soak-test.md").exists()


def test_every_line_carried_under_another_name_passes(tmp_path, monkeypatch):
    merged = CONDENSED + "\n## FD plateau soak test\n\n" + REFERENCE.split("\n", 2)[2]
    with _curator(tmp_path, monkeypatch) as root:
        _plant(root, "source", refs={"references/fd-plateau-soak-test.md": REFERENCE})
        _plant(root, "umbrella", refs={"references/fd-hygiene.md": merged})
        result = _remove("source", "references/fd-plateau-soak-test.md")
    assert result["success"] is True, result


def test_carried_but_still_linked_from_the_stub_is_refused(tmp_path, monkeypatch):
    link = "\nSee [soak](references/fd-plateau-soak-test.md).\n"
    with _curator(tmp_path, monkeypatch) as root:
        _plant(root, "source", body=link, refs={"references/fd-plateau-soak-test.md": REFERENCE})
        _plant(root, "umbrella", refs={"references/fd-plateau-soak-test.md": REFERENCE})
        result = _remove("source", "references/fd-plateau-soak-test.md")
    assert result["success"] is False, result
    assert "still links it" in result["error"]
    assert (root / "source" / "references" / "fd-plateau-soak-test.md").exists()


def test_link_repointed_to_the_umbrella_passes(tmp_path, monkeypatch):
    link = "\nSee [soak](../umbrella/references/fd-plateau-soak-test.md).\n"
    with _curator(tmp_path, monkeypatch) as root:
        _plant(root, "source", body=link, refs={"references/fd-plateau-soak-test.md": REFERENCE})
        _plant(root, "umbrella", refs={"references/fd-plateau-soak-test.md": REFERENCE})
        result = _remove("source", "references/fd-plateau-soak-test.md")
    assert result["success"] is True, result


def test_oversize_reference_fails_closed(tmp_path, monkeypatch):
    big = "Unique operational instruction.\n" * 100000
    with _curator(tmp_path, monkeypatch) as root:
        _plant(root, "source", refs={"references/large.md": big})
        result = _remove("source", "references/large.md")
    assert result["success"] is False, result
    assert result.get("_content_not_carried") is True
    assert (root / "source" / "references" / "large.md").exists()


def test_archive_copies_do_not_count_as_carried(tmp_path, monkeypatch):
    with _curator(tmp_path, monkeypatch) as root:
        _plant(root, "source", refs={"references/fd-plateau-soak-test.md": REFERENCE})
        (root / ".archive" / "old" / "references").mkdir(parents=True)
        (root / ".archive" / "old" / "references" / "fd-plateau-soak-test.md").write_text(REFERENCE)
        result = _remove("source", "references/fd-plateau-soak-test.md")
    assert result["success"] is False, result


def test_batch_fold_with_condensed_write_rolls_back(tmp_path, monkeypatch):
    with _curator(tmp_path, monkeypatch) as root:
        _plant(root, "source", refs={"references/fd-plateau-soak-test.md": REFERENCE})
        _plant(root, "umbrella")
        assert json.loads(skill_view("source", "references/fd-plateau-soak-test.md"))["success"]
        result = json.loads(skill_manage(action="", name="", operations=[
            {"action": "write_file", "name": "umbrella", "file_path": "references/fd-hygiene.md",
             "file_content": CONDENSED},
            {"action": "remove_file", "name": "source", "file_path": "references/fd-plateau-soak-test.md"},
        ]))
    assert result["success"] is False, result
    assert (root / "source" / "references" / "fd-plateau-soak-test.md").read_text() == REFERENCE
    assert not (root / "umbrella" / "references" / "fd-hygiene.md").exists()


def test_foreground_remove_is_not_gated(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    root = home / "skills"
    root.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    with patch("tools.skill_manager_tool.SKILLS_DIR", root), \
         patch("tools.skills_tool.SKILLS_DIR", root), \
         patch("agent.skill_utils.get_all_skills_dirs", return_value=[root]):
        _plant(root, "source", refs={"references/fd-plateau-soak-test.md": REFERENCE})
        result = json.loads(skill_manage(action="remove_file", name="source",
                                         file_path="references/fd-plateau-soak-test.md"))
    assert result["success"] is True, result


def test_constraint_stated_only_as_a_heading_must_be_carried(tmp_path, monkeypatch):
    ref = REFERENCE + "\n## Never run this migration against the production database\n"
    with _curator(tmp_path, monkeypatch) as root:
        _plant(root, "source", refs={"references/fd-plateau-soak-test.md": ref})
        _plant(root, "umbrella", refs={"references/fd-plateau-soak-test.md": REFERENCE + "\n## Usage\n"})
        result = _remove("source", "references/fd-plateau-soak-test.md")
    assert result["success"] is False, result
    assert result["uncarried_line_count"] == 1
    assert "Never run this migration" in result["error"]


def test_short_structural_heading_is_not_distinctive(tmp_path, monkeypatch):
    with _curator(tmp_path, monkeypatch) as root:
        _plant(root, "source", refs={"references/fd-plateau-soak-test.md": "## Usage\n## Notes\n" + REFERENCE})
        _plant(root, "umbrella", refs={"references/fd-plateau-soak-test.md": REFERENCE})
        result = _remove("source", "references/fd-plateau-soak-test.md")
    assert result["success"] is True, result


@pytest.mark.platforms("posix")
@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0, reason="root reads mode-000 files")
def test_unreadable_source_with_writable_parent_is_refused(tmp_path, monkeypatch):
    with _curator(tmp_path, monkeypatch) as root:
        _plant(root, "source", refs={"references/fd-plateau-soak-test.md": REFERENCE})
        target = root / "source" / "references" / "fd-plateau-soak-test.md"
        assert json.loads(skill_view("source", "references/fd-plateau-soak-test.md"))["success"]
        target.chmod(0)
        try:
            result = json.loads(skill_manage(action="remove_file", name="source",
                                             file_path="references/fd-plateau-soak-test.md"))
            assert target.exists()
        finally:
            target.chmod(0o644)
    assert result["success"] is False, result
    assert result.get("_content_not_carried") is True
    assert "could not be read" in result["error"]


def test_prose_with_one_cp1252_byte_is_still_checked(tmp_path, monkeypatch):
    raw = REFERENCE.replace("Kill the server", "Don\x92t skip it: kill the server").encode("latin-1")
    with _curator(tmp_path, monkeypatch) as root:
        _plant(root, "source")
        (root / "source" / "references").mkdir()
        (root / "source" / "references" / "fd-plateau-soak-test.md").write_bytes(raw)
        _plant(root, "umbrella", refs={"references/fd-hygiene.md": CONDENSED})
        result = _remove("source", "references/fd-plateau-soak-test.md")
    assert result["success"] is False, result
    assert result["uncarried_line_count"] >= 4
    assert (root / "source" / "references" / "fd-plateau-soak-test.md").read_bytes() == raw


def test_binary_file_without_identical_copy_is_refused(tmp_path, monkeypatch):
    with _curator(tmp_path, monkeypatch) as root:
        _plant(root, "source")
        (root / "source" / "assets").mkdir()
        (root / "source" / "assets" / "logo.bin").write_bytes(b"\x89PNG\0\0unique")
        guards.mark_background_review_skill_read(root / "source" / "assets" / "logo.bin")
        refused = json.loads(skill_manage(action="remove_file", name="source", file_path="assets/logo.bin"))
        _plant(root, "umbrella")
        (root / "umbrella" / "assets").mkdir()
        (root / "umbrella" / "assets" / "logo.bin").write_bytes(b"\x89PNG\0\0unique")
        allowed = json.loads(skill_manage(action="remove_file", name="source", file_path="assets/logo.bin"))
    assert refused["success"] is False and refused.get("_content_not_carried") is True, refused
    assert allowed["success"] is True, allowed


def test_concurrent_removals_of_identical_copies_leave_one(tmp_path, monkeypatch):
    rel = "references/fd-plateau-soak-test.md"
    real_candidates = guards._carry_candidates
    barrier = threading.Barrier(2, timeout=2)

    def _racing_candidates(target):
        found = real_candidates(target)
        try:  # hold both checks open together; under the fence the second never arrives
            barrier.wait()
        except threading.BrokenBarrierError:
            pass
        return found

    with _curator(tmp_path, monkeypatch) as root:
        _plant(root, "alpha", refs={rel: REFERENCE})
        _plant(root, "beta", refs={rel: REFERENCE})
        for name in ("alpha", "beta"):
            assert json.loads(skill_view(name, rel))["success"]
        results = {}
        ctx = contextvars.copy_context()

        def _run(name):
            results[name] = json.loads(ctx.copy().run(
                skill_manage, action="remove_file", name=name, file_path=rel))

        with patch.object(guards, "_carry_candidates", _racing_candidates):
            threads = [threading.Thread(target=_run, args=(n,)) for n in ("alpha", "beta")]
            for t in threads:
                t.start()
            for t in threads:
                t.join(30)
        survivors = [n for n in ("alpha", "beta") if (root / n / rel).exists()]
    assert sum(r["success"] for r in results.values()) <= 1, results
    assert len(survivors) == 1, results
