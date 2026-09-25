"""Subagent compact skill index + per-task skill promotion.

A delegate_task child inherits the parent's ENTIRE skills index for descriptions it
rarely reads. Children get a names-only index by default (the ``"*"`` sentinel in
``compact_categories``, same demote-never-hide contract as the coding posture), and
``tasks[i].skills`` re-promotes named skills to full descriptions.
"""

from types import SimpleNamespace
from unittest import mock

import pytest

from agent import prompt_builder as pb

_DESC = "A long, detailed description"


@pytest.fixture(autouse=True)
def skills_tree():
    pb.clear_skills_system_prompt_cache(clear_snapshot=True)
    root = pb.get_skills_dir()
    for c in range(4):
        for s in range(6):
            d = root / f"cat{c}" / f"skill-{c}-{s}"
            d.mkdir(parents=True)
            (d / "SKILL.md").write_text(
                f"---\nname: skill-{c}-{s}\n"
                f"description: \"{_DESC} for skill {c}-{s}: endpoints, workflows, pitfalls and "
                "conventions that cost tokens in every prompt that includes them.\"\n---\n\n# body\n"
            )
    yield
    pb.clear_skills_system_prompt_cache(clear_snapshot=True)


def _index(s: str) -> str:
    i = s.find("<available_skills>")
    return s[i:] if i >= 0 else s


def test_star_demotes_every_category_but_hides_nothing():
    full = pb.build_skills_system_prompt()
    compact = pb.build_skills_system_prompt(compact_categories=frozenset({"*"}))
    assert _DESC in full
    assert _DESC not in compact
    for c in range(4):
        assert f"cat{c} [names only]" in compact
        for s in range(6):
            assert f"skill-{c}-{s}" in compact
    assert len(_index(compact)) < len(_index(full)) * 0.5
    assert "compact index" in compact and "skill_view" in compact


def test_promoted_skills_keep_descriptions_inside_demoted_categories():
    out = pb.build_skills_system_prompt(
        compact_categories=frozenset({"*"}), promoted_skills=frozenset({"skill-1-2", "skill-3-0"}),
    )
    assert f"skill-1-2: {_DESC}" in out
    assert f"skill-3-0: {_DESC}" in out
    assert f"skill-1-3: {_DESC}" not in out
    assert "skill-1-3" in out


def test_promoted_set_is_part_of_the_cache_key():
    a = pb.build_skills_system_prompt(compact_categories=frozenset({"*"}))
    b = pb.build_skills_system_prompt(compact_categories=frozenset({"*"}), promoted_skills=frozenset({"skill-0-0"}))
    assert a != b
    assert pb.build_skills_system_prompt(compact_categories=frozenset({"*"})) == a


def test_named_category_demotion_unchanged():
    out = pb.build_skills_system_prompt(compact_categories=frozenset({"cat0"}))
    assert "cat0 [names only]" in out
    assert f"skill-1-0: {_DESC}" in out
    assert "outside the current coding" in out


def _agent(platform, delegate_skills=()):
    return SimpleNamespace(
        valid_tool_names={"skill_view", "skills_list"}, platform=platform, _delegate_skills=delegate_skills,
    )


def _skills_prompt(agent, delegation_cfg):
    from agent import system_prompt as sp

    with mock.patch("tools.delegate_tool_config._load_config", return_value=delegation_cfg), \
            mock.patch("agent.coding_context.coding_compact_skill_categories", return_value=frozenset()), \
            mock.patch.object(sp, "_agent_skills_dir", return_value=None):
        return sp._skills_prompt(agent)


def test_subagent_gets_compact_index_with_promoted_skills():
    out = _skills_prompt(_agent("subagent", ("skill-2-1",)), {})
    assert f"skill-2-1: {_DESC}" in out
    assert f"skill-2-2: {_DESC}" not in out
    assert "skill-2-2" in out


def test_parent_and_opted_out_subagent_keep_full_index():
    assert f"skill-2-2: {_DESC}" in _skills_prompt(_agent("cli"), {})
    assert f"skill-2-2: {_DESC}" in _skills_prompt(_agent("subagent"), {"compact_skill_index": False})


def test_build_children_attaches_per_task_skills():
    from tools import delegate_tool as dt

    built = []

    def _fake_build(**kw):
        child = SimpleNamespace()
        built.append(child)
        return child

    creds = {"provider": None, "base_url": None, "api_key": None, "api_mode": None, "model": None}
    tasks = [{"goal": "a", "skills": ["skill-0-1", " ", 7, "skill-0-2 "]}, {"goal": "b"}]
    with mock.patch.object(dt, "_build_child_preserving_parent_tools", side_effect=_fake_build):
        children, err = dt._build_children(
            tasks, [None, None], creds, top_role="leaf", max_iterations=5, parent_agent=SimpleNamespace(),
            routing_cfg={}, live_deleg_id=None, live_writers=[],
        )
    assert err is None
    assert built[0]._delegate_skills == ("skill-0-1", "skill-0-2")
    assert getattr(built[1], "_delegate_skills", ()) == ()


def test_schema_advertises_per_task_skills():
    from tools.delegate_tool import DELEGATE_TASK_SCHEMA

    item = DELEGATE_TASK_SCHEMA["parameters"]["properties"]["tasks"]["items"]["properties"]
    assert item["skills"]["type"] == "array"
