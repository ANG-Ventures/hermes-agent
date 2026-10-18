"""Session-scoped skill groups: ``<session scratch>/skill-groups`` makes an UNSUBSCRIBED sibling group
resolvable by name in skill_view for that session only, without touching config.yaml.

Real resolution chain (config.yaml -> get_external_skills_dirs -> _skill_search_dirs -> skill_view)
against a temp HERMES_HOME; nothing patched but the home and the session id."""

import json
from pathlib import Path

import pytest


@pytest.fixture
def fleet(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    (home / "skills").mkdir(parents=True)
    shared = tmp_path / "skills-shared"
    for group, name in (("general", "subscribed-skill"), ("gaming", "game-skill"), ("_lib", "lib-skill")):
        d = shared / group / name
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text("---\nname: %s\ndescription: %s test skill.\n---\n\n# %s\n" % (name, group, name))
    outside = tmp_path / "elsewhere" / "rogue"
    outside.mkdir(parents=True)
    (outside / "rogue-skill").mkdir()
    (outside / "rogue-skill" / "SKILL.md").write_text("---\nname: rogue-skill\ndescription: x.\n---\n")
    (home / "config.yaml").write_text("skills:\n  external_dirs:\n    - %s\n" % (shared / "general"))
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_SESSION_ID", "20261018_000000_abc123")
    monkeypatch.delenv("_HERMES_GATEWAY", raising=False)
    import hermes_constants
    monkeypatch.setattr(hermes_constants, "_hermes_home_cache", None, raising=False)
    monkeypatch.setattr(hermes_constants, "_default_hermes_root_memo", None, raising=False)
    from agent import skill_utils as su
    su._raw_config_cache_clear()
    su._external_dirs_cache_clear()
    import tools.skills_tool as skills_tool
    monkeypatch.setattr(skills_tool, "SKILLS_DIR", home / "skills")
    scratch = hermes_constants.get_session_scratch_dir("20261018_000000_abc123")
    yield {"home": home, "shared": shared, "outside": outside, "scratch": scratch, "tool": skills_tool}
    su._external_dirs_cache_clear()


def _view(fleet, name):
    return json.loads(fleet["tool"].skill_view(name, preprocess=False))


def test_unsubscribed_group_not_found_without_session_file(fleet):
    assert _view(fleet, "subscribed-skill")["success"] is True
    assert _view(fleet, "game-skill")["success"] is False


def test_session_file_makes_sibling_group_resolvable(fleet):
    (fleet["scratch"] / "skill-groups").write_text(str(fleet["shared"] / "gaming") + "\n")
    r = _view(fleet, "game-skill")
    assert r["success"] is True, r
    assert Path(r["skill_dir"]) == (fleet["shared"] / "gaming" / "game-skill").resolve()
    assert "skill-groups" not in (fleet["home"] / "config.yaml").read_text()


def test_session_file_is_scoped_to_its_session(fleet, monkeypatch):
    (fleet["scratch"] / "skill-groups").write_text(str(fleet["shared"] / "gaming") + "\n")
    monkeypatch.setenv("HERMES_SESSION_ID", "20261018_000000_other0")
    assert _view(fleet, "game-skill")["success"] is False


def test_non_sibling_and_underscore_dirs_are_ignored(fleet):
    (fleet["scratch"] / "skill-groups").write_text(
        "%s\n%s\nrelative/path\n" % (fleet["outside"], fleet["shared"] / "_lib"))
    assert _view(fleet, "rogue-skill")["success"] is False
    assert _view(fleet, "lib-skill")["success"] is False
