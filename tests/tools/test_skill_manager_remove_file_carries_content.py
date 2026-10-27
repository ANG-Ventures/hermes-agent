"""A background curator remove_file must not lose a reference file's content.

The weekly curator fold condensed reference files into an umbrella and then remove_file'd the
originals, so every line the condensation dropped existed nowhere on disk and the source stub kept
linking the removed path. The remove is allowed only once every distinctive line is carried by
another skill file and the source SKILL.md no longer links it."""

import json
from contextlib import contextmanager
from unittest.mock import patch

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
    merged = CONDENSED + "\n## Soak\n\n" + REFERENCE.split("\n", 2)[2]
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
