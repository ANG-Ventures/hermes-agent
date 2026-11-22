"""skill_manage patch/write_file/remove_file (and a read-only skill_view) reach a skill in a shared
group the profile does not mount: skills-shared is a library, not a loadout.

Fleet-shaped temp root: ``<root>/skills-shared`` (a git repo, as on the fleet) with one MOUNTED group
(``devops``, listed in get_all_skills_dirs) and unmounted ones (``media-aux``, ``research-aux``).
"""

import json
import subprocess
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

import pytest

import tools.skill_manager_tool as smt
from tools.skill_manager_tool import skill_manage

_TIER_STUB = "def load():\n    return []\ndef tier(path, rules):\n    return 'prose'\n"


def _skill(dir_: Path, name: str, body: str = "Step 1: do the thing.") -> Path:
    d = dir_ / name
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(f"---\nname: {name}\ndescription: Use when testing {name}.\n---\n\n# {name}\n\n{body}\n")
    return d


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout


@contextmanager
def _fleet(tmp_path):
    root = tmp_path / "root"
    home = root / "profiles" / "athena"
    local = home / "skills"
    shared = root / "skills-shared"
    local.mkdir(parents=True)
    (root / "scripts" / "lib").mkdir(parents=True)
    (root / "scripts" / "lib" / "autocommit_tier.py").write_text(_TIER_STUB)
    _skill(shared / "devops", "mounted-skill")
    _skill(shared / "media-aux", "audio-cleanup-separation")
    _skill(shared / "media-aux", "twin")
    _skill(shared / "research-aux", "twin")
    _skill(shared / "_bindings", "plumbing")
    _git(shared, "init", "-q")
    _git(shared, "-c", "user.email=t@t", "-c", "user.name=t", "add", "-A")
    _git(shared, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "seed")
    with patch.object(smt, "HERMES_HOME", home), patch.object(smt, "SKILLS_DIR", local), \
         patch("agent.skill_utils.get_all_skills_dirs", return_value=[local, shared / "devops"]):
        yield root, local, shared


def _op(**op):
    return json.loads(skill_manage(action="", name="", operations=[op]))


def test_patch_unmounted_skill_lands_in_shared_tree_and_git_sees_it(tmp_path):
    with _fleet(tmp_path) as (_root, local, shared):
        r = _op(action="patch", name="audio-cleanup-separation",
                old_string="Step 1: do the thing.", new_string="Step 1: do the better thing.")
        assert r["success"] is True, r
        target = shared / "media-aux" / "audio-cleanup-separation" / "SKILL.md"
        assert "do the better thing" in target.read_text()
        assert not any(local.rglob("SKILL.md")), "nothing was copied into the profile"
        # The autocommit lane keys off the shared repo's working tree: the write must show there.
        assert _git(shared, "status", "--porcelain").split() == [
            "M", "media-aux/audio-cleanup-separation/SKILL.md"]


def test_write_and_remove_file_on_unmounted_skill(tmp_path):
    with _fleet(tmp_path) as (_root, _local, shared):
        r = _op(action="write_file", name="audio-cleanup-separation",
                file_path="references/notes.md", file_content="# notes\n")
        assert r["success"] is True, r
        ref = shared / "media-aux" / "audio-cleanup-separation" / "references" / "notes.md"
        assert ref.read_text() == "# notes\n"
        r = _op(action="remove_file", name="audio-cleanup-separation", file_path="references/notes.md")
        assert r["success"] is True, r
        assert not ref.exists()


def test_ambiguous_name_refuses_with_both_qualified_names(tmp_path):
    with _fleet(tmp_path) as (_root, _local, shared):
        before = {p: p.read_text() for p in shared.rglob("SKILL.md")}
        r = _op(action="patch", name="twin", old_string="Step 1", new_string="Step 2")
        assert r["success"] is False
        assert "'media-aux/twin'" in r["error"] and "'research-aux/twin'" in r["error"], r
        assert {p: p.read_text() for p in shared.rglob("SKILL.md")} == before


def test_qualified_name_picks_one(tmp_path):
    with _fleet(tmp_path) as (_root, _local, shared):
        r = _op(action="patch", name="research-aux/twin", old_string="Step 1", new_string="Step 2")
        assert r["success"] is True, r
        assert "Step 2" in (shared / "research-aux" / "twin" / "SKILL.md").read_text()
        assert "Step 1" in (shared / "media-aux" / "twin" / "SKILL.md").read_text()


def test_mounted_skill_resolves_as_today(tmp_path):
    with _fleet(tmp_path) as (_root, _local, shared):
        assert smt._find_skill("mounted-skill") == {"path": shared / "devops" / "mounted-skill"}
        skill_dir, err = smt._locate_for_write("mounted-skill", "patch")
        assert (skill_dir, err) == (shared / "devops" / "mounted-skill", None)


def test_delete_and_loading_do_not_reach_the_library(tmp_path):
    with _fleet(tmp_path) as (_root, _local, shared):
        r = _op(action="delete", name="audio-cleanup-separation")
        assert r["success"] is False and "not found" in r["error"], r
        assert (shared / "media-aux" / "audio-cleanup-separation" / "SKILL.md").is_file()
        assert smt._find_skill("audio-cleanup-separation") is None
        assert smt.find_library_skill("plumbing") == (None, None)


def test_failed_batch_rolls_back_an_unmounted_skill(tmp_path):
    with _fleet(tmp_path) as (_root, _local, shared):
        target = shared / "media-aux" / "audio-cleanup-separation" / "SKILL.md"
        before = target.read_text()
        r = json.loads(skill_manage(action="", name="", operations=[
            {"action": "patch", "name": "audio-cleanup-separation",
             "old_string": "Step 1: do the thing.", "new_string": "Step 1: changed."},
            {"action": "patch", "name": "audio-cleanup-separation",
             "old_string": "no such text anywhere", "new_string": "x"}]))
        assert r["success"] is False, r
        assert target.read_text() == before


def test_skill_view_reads_unmounted_skill_read_only(tmp_path, monkeypatch):
    import tools.skills_tool as st
    with _fleet(tmp_path) as (_root, local, shared):
        monkeypatch.setattr(st, "_skill_search_dirs", lambda: ([], [local, shared / "devops"], local))
        r = json.loads(st.skill_view("audio-cleanup-separation"))
        assert r["success"] is True and r["library"] is True, r
        assert "Step 1: do the thing." in r["content"]
        assert Path(r["skill_dir"]) == shared / "media-aux" / "audio-cleanup-separation"
        amb = json.loads(st.skill_view("twin"))
        assert amb["success"] is False and "'research-aux/twin'" in amb["error"]
        assert json.loads(st.skill_view("no-such-skill"))["error"] == "Skill 'no-such-skill' not found."
        mounted = json.loads(st.skill_view("mounted-skill", preprocess=False))
        assert mounted["success"] is True and "library" not in mounted


def test_skill_index_is_byte_identical(tmp_path, monkeypatch):
    """Nothing joins the profile's available-skills index: same bytes before and after a library
    view + patch of an unmounted skill."""
    import tools.skills_tool as st
    from agent import prompt_builder
    with _fleet(tmp_path) as (_root, local, shared):
        monkeypatch.setattr(st, "_skill_search_dirs", lambda: ([], [local, shared / "devops"], local))
        monkeypatch.setattr(prompt_builder, "get_all_skills_dirs", lambda: [local, shared / "devops"])

        def index():
            prompt_builder.clear_skills_system_prompt_cache(clear_snapshot=True)
            return prompt_builder.build_skills_system_prompt(skills_dir_override=local)

        before = index()
        assert "mounted-skill" in before and "audio-cleanup-separation" not in before
        st.skill_view("audio-cleanup-separation")
        assert _op(action="patch", name="audio-cleanup-separation",
                   old_string="Step 1", new_string="Step 9")["success"] is True
        assert index() == before


def test_background_review_keeps_its_scope(tmp_path):
    """The autonomous curator fork gets no library fallback: an unmounted skill stays not-found."""
    with _fleet(tmp_path) as (_root, _local, shared), \
            patch.object(smt, "_is_background_review", return_value=True):
        skill_dir, err = smt._locate_for_write("audio-cleanup-separation", "patch")
        assert skill_dir is None and "not found" in err["error"], err
        assert "Step 1: do the thing." in (
            shared / "media-aux" / "audio-cleanup-separation" / "SKILL.md").read_text()


def test_symlinked_package_out_of_the_library_is_not_a_hit(tmp_path):
    with _fleet(tmp_path) as (_root, _local, shared):
        outside = _skill(tmp_path / "outside", "escapee")
        (shared / "media-aux" / "escapee").symlink_to(outside, target_is_directory=True)
        assert smt.find_library_skill("escapee") == (None, None)
        assert smt.find_library_skill("media-aux/escapee") == (None, None)
        # package dir out of the library, its SKILL.md a symlink back in: still not a hit.
        back = tmp_path / "outside2"
        back.mkdir()
        (back / "SKILL.md").symlink_to(shared / "media-aux" / "audio-cleanup-separation" / "SKILL.md")
        (shared / "media-aux" / "boomerang").symlink_to(back, target_is_directory=True)
        assert smt.find_library_skill("boomerang") == (None, None)


def test_skill_view_library_fallback_with_empty_loadout(tmp_path, monkeypatch):
    import tools.skills_tool as st
    with _fleet(tmp_path) as (_root, local, _shared):
        monkeypatch.setattr(st, "_skill_search_dirs", lambda: ([], [], local))
        r = json.loads(st.skill_view("audio-cleanup-separation"))
        assert r["success"] is True and r["library"] is True, r
        miss = json.loads(st.skill_view("no-such-skill"))
        assert miss["success"] is False and "does not exist yet" in miss["error"], miss
