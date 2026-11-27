"""skill_manage per-call discovery memo: fewer discoveries per public call, never a stale answer.

The gateway profile showed ``_iter_skill_dirs`` (a full rglob over ~1.7k SKILL.md) at ~10% of GIL
samples because one skill_manage call resolved the same name up to four times. The memo lets ONE
public call reuse its own first discovery of a name; it is emptied after the handler runs and
never outlives the call. Every discovery is main's full walk: there is no cross-call cache.
"""

import json
import shutil
import threading
import types
from contextlib import ExitStack, contextmanager
from unittest.mock import patch

import tools.skill_manager_tool as smt


def _mk(root, rel, name=None):
    d = root / rel
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {name or d.name}\ndescription: Use when testing. Does a thing.\n---\n\n# x\n\nold line\n",
        encoding="utf-8")
    return d


@contextmanager
def _roots(local, *external):
    with patch("tools.skill_manager_tool.SKILLS_DIR", local), \
         patch("agent.skill_utils.get_all_skills_dirs", return_value=[local, *external]):
        yield


@contextmanager
def _public_env(home, local, *external):
    """Real guards, ledger and post-capture; only the approval gate, the cross-profile scan and
    the sync push are off."""
    with ExitStack() as stack:
        stack.enter_context(_roots(local, *external))
        stack.enter_context(patch.object(smt, "HERMES_HOME", home))
        stack.enter_context(patch.object(smt, "_apply_skill_write_gate", return_value=None))
        stack.enter_context(patch.object(smt, "_find_skill_in_other_profiles", return_value=[]))
        stack.enter_context(patch.object(smt, "_maybe_debounced_sync_push"))
        yield


def _call(**kw):
    return json.loads(smt.skill_manage(**kw))


# ---------------------------------------------------------------- lookup semantics (main's discovery)

def test_external_create_delete_rename_seen_on_next_lookup(tmp_path):
    local = tmp_path / "skills"
    d = _mk(local, "devops/alpha")
    with _roots(local):
        assert smt._find_skill("beta") is None
        _mk(local, "devops/beta")
        assert smt._find_skill("beta")["path"] == local / "devops" / "beta"
        d.rename(local / "devops" / "gamma")
        assert smt._find_skill("alpha") is None
        assert smt._find_skill("gamma")["path"] == local / "devops" / "gamma"
        shutil.rmtree(local / "devops" / "gamma")
        assert smt._find_skill("gamma") is None


def test_categorized_external_and_excluded_names(tmp_path):
    local, ext = tmp_path / "skills", tmp_path / "shared" / "devops"
    _mk(local, "mlops/axolotl")
    _mk(ext, "only-ext")
    _mk(ext, "both")
    _mk(local, "cat/both")
    _mk(local, ".git/hidden")
    _mk(local, "mlops/axolotl/references/sub")
    with _roots(local, ext):
        assert smt._find_skill("mlops/axolotl")["path"] == local / "mlops" / "axolotl"
        assert smt._find_skill("axolotl")["path"] == local / "mlops" / "axolotl"
        assert smt._find_skill("devops/axolotl") is None
        assert smt._find_skill("only-ext")["path"] == ext / "only-ext"
        assert smt._find_skill("both")["path"] == local / "cat" / "both"
        assert smt._find_skill("devops/only-ext") is None
        assert smt._find_skill("hidden") is None
        assert smt._find_skill("sub") is None


def test_outside_a_public_call_every_lookup_discovers(tmp_path):
    local = tmp_path / "skills"
    _mk(local, "c/alpha")
    with _roots(local), patch.object(smt, "_discover_skill", wraps=smt._discover_skill) as disc:
        for _ in range(3):
            assert smt._find_skill("alpha")
        assert disc.call_count == 3


# ---------------------------------------------------------------- discoveries per public call

_CREATE = "---\nname: fresh\ndescription: Use when testing. Does a thing.\n---\n\n# F\n"
_OPS = [
    ("patch hit", dict(action="patch", name="s0", old_string="old line", new_string="new line"), True),
    ("patch miss", dict(action="patch", name="absent", old_string="a", new_string="b"), False),
    ("create", dict(action="create", name="fresh", category="c", local=True, content=_CREATE), True),
    ("delete", dict(action="delete", name="fresh"), True),
    ("write_file", dict(action="write_file", name="s1", file_path="references/r.md", file_content="r"), True),
    ("batch of 3 patches", dict(action="patch", name="s0", operations=[
        {"action": "patch", "name": n, "old_string": "old line", "new_string": "new line"}
        for n in ("s1", "s2", "s3")]), True),
]


def count_discoveries(home, arm):
    """Discoveries (full walks) per public operation. ``main`` binds main's ``_find_skill`` (no
    memo) into the same dispatcher, so the two arms differ only by the memo."""
    from tests.tools import review171_legacy_lookup as legacy
    local = home / "skills"
    for i in range(4):
        _mk(local, f"c/s{i}")
    counts = {}
    with _public_env(home, local), ExitStack() as stack:
        if arm == "main":
            spy = stack.enter_context(patch.object(smt, "_find_skill", side_effect=types.FunctionType(
                legacy._find_skill.__code__, smt.__dict__)))
        else:
            spy = stack.enter_context(patch.object(smt, "_discover_skill", wraps=smt._discover_skill))
        for label, kw, ok in _OPS:
            before = spy.call_count
            out = _call(**kw)
            assert bool(out.get("success")) is ok, (arm, label, out)
            counts[label] = spy.call_count - before
    return counts


def test_discoveries_per_public_call_head_vs_main(tmp_path):
    head = count_discoveries(tmp_path / "head", "head")
    main = count_discoveries(tmp_path / "main", "main")
    print(f"\ndiscoveries per call: main={main} head={head}")
    assert all(head[k] <= main[k] for k in head), (head, main)
    # A successful single-name mutation: the preflight's discovery (before the lock), one under
    # the lock shared by the ledger pre-capture and the handler, one after the handler.
    assert head["patch hit"] == head["write_file"] == 3
    assert head["patch miss"] == 2
    assert head["patch hit"] < main["patch hit"]


def test_mutation_inside_call_clears_whole_memo(tmp_path):
    """A higher-precedence copy appears during the handler: post-capture rediscovers, never reuses."""
    local, ext = tmp_path / "skills", tmp_path / "ext"
    ext_skill = _mk(ext, "dup")
    seen = []
    real_handler = smt._ACTION_HANDLERS["patch"]

    def handler(a):
        out = real_handler(a)
        _mk(local, "c/dup")
        return out

    def record(action, name, *a, **kw):
        seen.append(smt._find_skill(name)["path"])

    with _public_env(tmp_path, local, ext), \
         patch.dict(smt._ACTION_HANDLERS, {"patch": handler}), \
         patch.object(smt, "_record_success", side_effect=record):
        out = _call(action="patch", name="dup", old_string="old line", new_string="new line")
    assert out["success"], out
    assert "new line" in (ext_skill / "SKILL.md").read_text(encoding="utf-8-sig")
    assert seen == [local / "c" / "dup"]


def test_memo_does_not_survive_the_call(tmp_path):
    local, ext = tmp_path / "skills", tmp_path / "ext"
    _mk(ext, "dup")
    with _public_env(tmp_path, local, ext):
        assert _call(action="patch", name="dup", old_string="old line", new_string="new line")["success"]
        override = _mk(local, "c/dup")  # another process, between calls
        assert _call(action="patch", name="dup", old_string="old line", new_string="new line")["success"]
    assert "new line" in (override / "SKILL.md").read_text(encoding="utf-8-sig")
    assert smt._SKILL_OPERATION_MEMO.get() is None


def test_two_threads_never_share_a_memo(tmp_path):
    """Thread A holds a memo entry for ``dup`` (external copy) mid-call. Thread B's concurrent
    call (on another skill, so it does not wait on A's per-name lock) and A's context copied into
    a third thread must both discover ``dup`` fresh (the local override created meanwhile).

    The one remaining difference from main: inside the lock, a name's answer is fixed from lock
    entry until the handler returns, so a writer that bypasses the lock (here the override written
    mid-handler) is seen one discovery later than on main; main's handler would patch the override."""
    import contextvars
    local, ext = tmp_path / "skills", tmp_path / "ext"
    _mk(ext, "dup")
    _mk(local, "c/other")
    in_handler, release = threading.Event(), threading.Event()
    copied, results, seen = {}, {}, {}
    real_handler = smt._ACTION_HANDLERS["patch"]

    def handler(a):
        if threading.current_thread().name == "A":
            seen["A-before"] = smt._find_skill("dup")["path"]
            copied["ctx"] = contextvars.copy_context()
            in_handler.set()
            assert release.wait(10)
        else:
            seen["B"] = smt._find_skill("dup")["path"]
        return real_handler(a)

    def run(name):
        results[threading.current_thread().name] = _call(
            action="patch", name=name, old_string="old line", new_string="new line")

    with _public_env(tmp_path, local, ext), patch.dict(smt._ACTION_HANDLERS, {"patch": handler}):
        a = threading.Thread(target=run, args=("dup",), name="A")
        a.start()
        try:
            assert in_handler.wait(10)
            override = _mk(local, "c/dup")
            t = threading.Thread(target=lambda: seen.update(
                copied=copied["ctx"].run(smt._find_skill, "dup")["path"]))
            t.start()
            t.join(10)
            b = threading.Thread(target=run, args=("other",), name="B")
            b.start()
            b.join(10)
        finally:
            release.set()
            a.join(10)
    assert seen["A-before"] == ext / "dup"
    assert seen["copied"] == override and seen["B"] == override, seen
    assert results["B"]["success"] and results["A"]["success"], results
    # A stays bound to its own first answer for its whole call: its patch lands on the external copy.
    assert "new line" in (ext / "dup" / "SKILL.md").read_text(encoding="utf-8-sig")
    assert "new line" not in (override / "SKILL.md").read_text(encoding="utf-8-sig")
