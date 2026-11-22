"""A skill_manage batch resolves each named skill to its directory ONCE, before any op runs, and
uses that directory for the clobber guard, the snapshot and the rollback (t_3d5f0fb2).

Each test below is one Prism key from hermes-agent#523, driven through skill_manage(operations=...).
"""

import json
import os
from unittest.mock import patch

import tools.skill_manager_tool as smt
from tools.skill_manager_tool import skill_manage

from tests.tools.test_skill_manage_library_fallback import _fleet

_MISS = {"action": "patch", "old_string": "text that is not in the file", "new_string": "x"}


def _batch(*ops):
    return json.loads(skill_manage(action="", name="", operations=list(ops)))


def test_rollback_restores_a_removed_skill_md(tmp_path):
    """879e247abb4f: remove_file SKILL.md, then a failing op. The name no longer resolves after op 0,
    so a by-name rollback found nothing and copytree hit the still-present dir (Errno 17)."""
    with _fleet(tmp_path) as (_root, _local, shared):
        skill_md = shared / "devops" / "mounted-skill" / "SKILL.md"
        before = skill_md.read_text()
        r = _batch({"action": "remove_file", "name": "mounted-skill", "file_path": "SKILL.md"},
                   {**_MISS, "name": "mounted-skill"})
        assert r["success"] is False, r
        assert "ROLLBACK FAILED" not in r["error"], r["error"]
        assert skill_md.read_text() == before


def test_op_that_raises_rolls_back_and_reports(tmp_path):
    """53b1eb27200b: an op that RAISES is handled like one that returns a failure."""
    with _fleet(tmp_path) as (_root, _local, shared):
        skill_md = shared / "devops" / "mounted-skill" / "SKILL.md"
        before = skill_md.read_text()
        real = smt._skill_manage_from
        calls = []

        def flaky(payload, **kw):
            calls.append(payload["action"])
            if len(calls) == 2:
                raise RuntimeError("boom")
            return real(payload, **kw)

        with patch.object(smt, "_skill_manage_from", flaky):
            r = _batch({"action": "patch", "name": "mounted-skill",
                        "old_string": "Step 1: do the thing.", "new_string": "Step 1: changed."},
                       {"action": "write_file", "name": "mounted-skill",
                        "file_path": "references/a.md", "file_content": "a\n"})
        assert r["success"] is False and r["failed_index"] == 1, r
        assert "boom" in r["error"] and "rolled back" in r["error"], r["error"]
        assert skill_md.read_text() == before


def test_rollback_never_touches_a_skill_the_batch_did_not_resolve(tmp_path):
    """a85b8aa288a4: 'twin' is in two unmounted groups. After op 0 removes media-aux/twin's SKILL.md
    the bare name resolves to research-aux/twin, which a by-name rollback then rmtree()d."""
    with _fleet(tmp_path) as (_root, _local, shared):
        a = shared / "media-aux" / "twin" / "SKILL.md"
        b = shared / "research-aux" / "twin" / "SKILL.md"
        before_a, before_b = a.read_text(), b.read_text()
        r = _batch({"action": "remove_file", "name": "media-aux/twin", "file_path": "SKILL.md"},
                   {**_MISS, "name": "twin"})
        assert r["success"] is False, r
        assert b.read_text() == before_b, "the other group's skill must survive"
        assert a.read_text() == before_a


def test_rollback_keeps_symlinks(tmp_path):
    """c580e8a79615: the snapshot copy followed links, so a failed batch turned a symlink into a file."""
    with _fleet(tmp_path) as (_root, _local, shared):
        pkg = shared / "devops" / "mounted-skill"
        (pkg / "references").mkdir()
        (pkg / "references" / "real.md").write_text("real\n")
        link = pkg / "references" / "link.md"
        os.symlink("real.md", link)
        r = _batch({"action": "write_file", "name": "mounted-skill",
                    "file_path": "references/new.md", "file_content": "n\n"},
                   {**_MISS, "name": "mounted-skill"})
        assert r["success"] is False, r
        assert link.is_symlink() and os.readlink(link) == "real.md"
        assert not (pkg / "references" / "new.md").exists()


def test_clobber_guard_sees_one_skill_under_two_spellings(tmp_path):
    """3b3d7448c511: bare and qualified names of one skill passed the name-keyed clobber guard."""
    with _fleet(tmp_path) as (_root, _local, shared):
        ref = shared / "media-aux" / "audio-cleanup-separation" / "references" / "a.md"
        r = _batch({"action": "write_file", "name": "audio-cleanup-separation",
                    "file_path": "references/a.md", "file_content": "first\n"},
                   {"action": "write_file", "name": "media-aux/audio-cleanup-separation",
                    "file_path": "references/a.md", "file_content": "second\n"})
        assert r["success"] is False and "already touched" in r["error"], r
        assert not ref.exists(), "a refused batch applies nothing"
