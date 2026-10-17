"""skill_manage refuses exec-tier writes/deletes inside the live skills-shared/ tree.

The exec globs come from ``<root>/scripts/lib/autocommit_tier.py`` (the fleet's one tier reader);
each test builds a temp Hermes root with a ``skills-shared/general/`` group, a profile home under
``profiles/``, and a fake tier module exposing the reader's ``load()`` / ``tier()`` API.
"""

import json
import logging
from contextlib import contextmanager
from unittest.mock import patch

import tools.skill_manager_tool as smt
from tools.skill_manager_tool import skill_manage

SK = "---\nname: {n}\ndescription: Use when probing exec refusal. Behavior.\n---\n# Probe\nStep 1.\n"

FAKE_TIER = '''
import re
EXEC = ("scripts/**", "**/*.py", "**/*.sh")
def _rx(g):
    out, i = [], 0
    while i < len(g):
        if g.startswith("**/", i): out.append("(?:.*/)?"); i += 3
        elif g.startswith("**", i): out.append(".*"); i += 2
        elif g[i] == "*": out.append("[^/]*"); i += 1
        else: out.append(re.escape(g[i])); i += 1
    return re.compile("".join(out), re.DOTALL)
def load():
    return [_rx(g) for g in EXEC]
def tier(path, rules):
    return "exec" if any(r.fullmatch(path) for r in rules) else "prose"
'''


@contextmanager
def _root(tmp_path, with_tier=True):
    root = tmp_path / "root"
    home = root / "profiles" / "daedalus"
    local = home / "skills"
    shared = root / "skills-shared"
    local.mkdir(parents=True)
    (shared / "general").mkdir(parents=True)
    if with_tier:
        (root / "scripts" / "lib").mkdir(parents=True)
        (root / "scripts" / "lib" / "autocommit_tier.py").write_text(FAKE_TIER)
    with patch.object(smt, "HERMES_HOME", home), \
         patch.object(smt, "SKILLS_DIR", local), \
         patch.object(smt, "_author_to_shared_setting", return_value=None), \
         patch("agent.skill_utils.get_all_skills_dirs", return_value=[local, shared / "general"]), \
         patch("tools.write_approval.write_approval_enabled", return_value=False):
        yield root, local, shared


def _ops(ops):
    return json.loads(skill_manage(action="", name="", operations=ops))


def _shared_skill(shared, name="probe", files=()):
    d = shared / "general" / name
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(SK.format(n=name))
    for rel in files:
        (d / rel).parent.mkdir(parents=True, exist_ok=True)
        (d / rel).write_text("print('x')\n")
    return d


def _refused(res):
    assert res["success"] is False, res
    assert "drain-pr --code" in res["error"] and "worktree" in res["error"], res["error"]


def test_s1_patch_shared_script_refused(tmp_path):
    with _root(tmp_path) as (_, _, shared):
        d = _shared_skill(shared, files=("scripts/x.py",))
        res = _ops([{"action": "patch", "name": "probe", "file_path": "scripts/x.py",
                     "old_string": "print('x')", "new_string": "print('y')"}])
        _refused(res)
        assert (d / "scripts/x.py").read_text() == "print('x')\n"


def test_s2_write_file_shared_script_refused(tmp_path):
    with _root(tmp_path) as (_, _, shared):
        d = _shared_skill(shared)
        _refused(_ops([{"action": "write_file", "name": "probe", "file_path": "scripts/x.py",
                        "file_content": "print(1)\n"}]))
        assert not (d / "scripts").exists()


def test_s3_flat_patch_with_local_true_still_refused(tmp_path):
    with _root(tmp_path) as (_, _, shared):
        d = _shared_skill(shared, files=("scripts/x.py",))
        res = json.loads(skill_manage("patch", "probe", file_path="scripts/x.py", old_string="print('x')",
                                      new_string="print('y')", local=True))
        _refused(res)
        assert (d / "scripts/x.py").read_text() == "print('x')\n"


def test_s4_local_create_plus_script_lands_locally(tmp_path):
    with _root(tmp_path) as (_, local, shared):
        # category is set: the batch precomputes create targets without `local`, so a categoryless
        # local create raises there before any op runs (separate bug, outside this card's scope).
        res = _ops([{"action": "create", "name": "loc", "content": SK.format(n="loc"), "local": True,
                     "category": "general"},
                    {"action": "write_file", "name": "loc", "file_path": "scripts/x.py",
                     "file_content": "print(1)\n"}])
        assert res["success"] is True, res
        assert (local / "general" / "loc" / "SKILL.md").is_file()
        assert (local / "general" / "loc" / "scripts" / "x.py").is_file()
        assert not (shared / "general" / "loc").exists()


def test_s5_write_file_reference_on_shared_allowed(tmp_path):
    with _root(tmp_path) as (_, _, shared):
        d = _shared_skill(shared)
        res = _ops([{"action": "write_file", "name": "probe", "file_path": "references/x.md",
                     "file_content": "notes\n"}])
        assert res["success"] is True, res
        assert (d / "references/x.md").read_text() == "notes\n"


def test_s6_shared_create_plus_script_rolls_back(tmp_path):
    with _root(tmp_path) as (_, _, shared):
        res = _ops([{"action": "create", "name": "newsk", "content": SK.format(n="newsk"),
                     "category": "general"},
                    {"action": "write_file", "name": "newsk", "file_path": "scripts/x.py",
                     "file_content": "print(1)\n"}])
        assert res["success"] is False, res
        assert res["failed_index"] == 1
        assert "drain-pr --code" in res["error"]
        assert not (shared / "general" / "newsk").exists()


def test_s7_shared_create_alone_allowed(tmp_path):
    with _root(tmp_path) as (_, _, shared):
        res = json.loads(skill_manage("create", "solo", content=SK.format(n="solo"), category="general"))
        assert res["success"] is True, res
        assert res["authored_to"] == "shared"
        d = shared / "general" / "solo"
        assert [p.relative_to(d).as_posix() for p in d.rglob("*")] == ["SKILL.md"]


def test_s8_remove_file_shared_script_refused(tmp_path):
    with _root(tmp_path) as (_, _, shared):
        d = _shared_skill(shared, files=("scripts/x.py",))
        _refused(_ops([{"action": "remove_file", "name": "probe", "file_path": "scripts/x.py"}]))
        assert (d / "scripts/x.py").is_file()


def test_s9_tier_reader_absent_with_shared_tree_refuses(tmp_path, caplog):
    # Prism P1 1c24bb177f50: with a skills-shared/ tree the reader is always deployed, so its
    # absence is a broken deploy and must not wave a write through.
    with _root(tmp_path, with_tier=False) as (root, _, shared):
        d = _shared_skill(shared)
        with caplog.at_level(logging.WARNING, logger="tools.skill_manager_tool"):
            res = _ops([{"action": "write_file", "name": "probe", "file_path": "references/x.md",
                         "file_content": "notes\n"}])
        assert res["success"] is False, res
        assert "tier reader missing" in res["error"] and "worktree + PR" in res["error"], res["error"]
        assert str(root / "scripts" / "lib" / "autocommit_tier.py") in res["error"]
        assert not (d / "references").exists()
        assert any("tier reader missing" in r.getMessage() for r in caplog.records)


def test_s9b_tier_reader_absent_without_shared_tree_allows(tmp_path):
    # True vanilla install: no skills-shared/ and no tier table -> legacy local writes still work.
    with _root(tmp_path, with_tier=False) as (_, local, shared):
        shared.rename(shared.with_name("gone"))
        d = local / "general" / "van"
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text(SK.format(n="van"))
        with patch("agent.skill_utils.get_all_skills_dirs", return_value=[local]):
            res = _ops([{"action": "write_file", "name": "van", "file_path": "scripts/x.py",
                         "file_content": "print(1)\n"}])
        assert res["success"] is True, res
        assert (d / "scripts" / "x.py").is_file()
        assert smt._exec_tier_refusal(d, d / "scripts" / "x.py", "write_file") is None


def test_s9c_tier_reader_broken_refuses(tmp_path):
    with _root(tmp_path) as (root, _, shared):
        (root / "scripts" / "lib" / "autocommit_tier.py").write_text("raise RuntimeError('boom')\n")
        d = _shared_skill(shared)
        res = _ops([{"action": "write_file", "name": "probe", "file_path": "references/x.md",
                     "file_content": "notes\n"}])
        assert res["success"] is False, res
        assert "unreadable" in res["error"] and "RuntimeError" in res["error"], res["error"]
        assert not (d / "references").exists()


def test_s10_exec_named_symlink_to_prose_still_refused(tmp_path):
    # Prism r1 P1: the REQUESTED path is classified, not only the symlink's resolved destination.
    with _root(tmp_path) as (_, _, shared):
        d = _shared_skill(shared)
        (d / "references").mkdir()
        (d / "references" / "x.md").write_text("notes\n")
        (d / "scripts").mkdir()
        (d / "scripts" / "x.sh").symlink_to("../references/x.md")
        assert smt._exec_tier_refusal(d, d / "scripts" / "x.sh", "write_file") is not None
        _refused(_ops([{"action": "write_file", "name": "probe", "file_path": "scripts/x.sh",
                        "file_content": "echo hi\n"}]))
        assert (d / "references" / "x.md").read_text() == "notes\n"


def test_s11_prose_symlink_to_prose_inside_skill_allowed(tmp_path):
    with _root(tmp_path) as (_, _, shared):
        d = _shared_skill(shared)
        (d / "references").mkdir()
        (d / "references" / "real.md").write_text("notes\n")
        (d / "references" / "alias.md").symlink_to("real.md")
        assert smt._exec_tier_refusal(d, d / "references" / "alias.md", "write_file") is None
        res = _ops([{"action": "write_file", "name": "probe", "file_path": "references/alias.md",
                     "file_content": "more\n"}])
        assert res["success"] is True, res
        assert (d / "references" / "real.md").read_text() == "more\n"


def test_s12_symlink_escaping_skills_shared_refused(tmp_path):
    # Prism P1 7f9b52bf26bc: a link whose target leaves skills-shared/ is refused whatever its tier.
    with _root(tmp_path) as (_, _, shared):
        d = _shared_skill(shared)
        outside = tmp_path / "elsewhere" / "notes.md"
        outside.parent.mkdir()
        outside.write_text("orig\n")
        (d / "references").mkdir()
        (d / "references" / "x.md").symlink_to(outside)
        res = _ops([{"action": "write_file", "name": "probe", "file_path": "references/x.md",
                     "file_content": "pwned\n"}])
        assert res["success"] is False, res
        assert "symlink escapes the skills tree" in res["error"], res["error"]
        assert outside.read_text() == "orig\n"


def test_s13_symlink_to_root_scripts_lib_refused(tmp_path):
    with _root(tmp_path) as (root, _, shared):
        d = _shared_skill(shared)
        real = root / "scripts" / "lib" / "real.py"
        real.write_text("print('live')\n")
        (d / "references").mkdir()
        (d / "references" / "x.md").symlink_to(real)
        assert smt._exec_tier_refusal(d, d / "references" / "x.md", "write_file") is not None
        res = _ops([{"action": "patch", "name": "probe", "file_path": "references/x.md",
                     "old_string": "print('live')", "new_string": "print('pwned')"}])
        assert res["success"] is False, res
        assert "symlink escapes the skills tree" in res["error"], res["error"]
        assert real.read_text() == "print('live')\n"
