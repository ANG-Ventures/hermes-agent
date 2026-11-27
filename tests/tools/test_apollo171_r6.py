"""Apollo round-6 independent probes for PR 171 at 2ff5efac6b (index-internal seams guarded by _HAS_INDEX).

Every probe drives the PUBLIC entrypoint smt.skill_manage and is parametrised head vs the main
rglob control (tests/tools/review171_legacy_lookup.py patched in as smt._find_skill). The oracle in
each probe is a FRESH main discovery taken at assertion time (``fresh()``), so a probe is red only
when the implementation under test disagrees with what main would return right now.
A ``model`` assertion before the decisive call proves the mutation really changed fresh discovery
(Seam 13: a probe whose mutation changed nothing proves nothing); on interpreters whose rglob
semantics make the mutation a no-op the probe SKIPS with the reason instead of passing.
"""
import json
import os
import shutil
import threading
import time
import types
from collections import Counter
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

import pytest

from tools import skill_manager_tool as smt
from tests.tools.test_skill_manager_index import _mk, _roots
from tests.tools import review171_legacy_lookup as legacy

_LEGACY = types.FunctionType(legacy._find_skill.__code__, smt.__dict__)
# Round-6 scope cut removed the cross-call index; probes that drove its internals take the
# main path (or skip, as main does) when it is absent.
_HAS_INDEX = hasattr(smt, '_SKILL_INDEX')


@pytest.fixture(autouse=True, params=['head', 'main-rglob-control'])
def impl(request):
    if request.param == 'head':
        yield 'head'
    else:
        with patch.object(smt, '_find_skill', _LEGACY):
            yield 'main'


def env(stack, home, *roots):
    for r in roots:
        r.mkdir(parents=True, exist_ok=True)
    (roots[0] / '.locks').mkdir(exist_ok=True)
    stack.enter_context(_roots(*roots))
    stack.enter_context(patch.object(smt, 'HERMES_HOME', home))
    for symbol, value in [('_apply_skill_write_gate', None), ('_background_review_preflight', None),
                          ('_record_success', None), ('_find_skill_in_other_profiles', [])]:
        stack.enter_context(patch.object(smt, symbol, return_value=value))


def fresh(name):
    """Main's answer right now (fresh rglob), independent of any cache."""
    found = _LEGACY(name)
    return found['path'] if found else None


def public_patch(name):
    return json.loads(smt.skill_manage(action='patch', name=name, old_string='# x',
                                       new_string='# changed'))


def lookup(name):
    """Public warm-up / lookup that mutates nothing (unknown action resolves the name first)."""
    return json.loads(smt.skill_manage(action='unknown-r6', name=name))


def assert_agrees_with_fresh(name):
    expected = fresh(name)
    result = public_patch(name)
    if expected is None:
        assert not result.get('success'), (
            f'fresh discovery finds no {name!r} but the public call succeeded: {result}')
        return
    assert result.get('success'), (expected, result)
    assert '# changed' in (expected / 'SKILL.md').read_text(encoding='utf-8-sig'), (
        f'fresh discovery resolves {name!r} to {expected}; the public call edited something else: {result}')


def _need_non_root():
    if hasattr(os, 'geteuid') and os.geteuid() == 0:
        pytest.skip('permission probes are vacuous as root')


# ---------------------------------------------------------------- (A) divergence probes

def test_r6_symlinked_skill_md_target_removed_unhides_support_skill(tmp_path):
    """foo/SKILL.md is a symlink to a file OUTSIDE the tree; foo/references/bar/SKILL.md is
    excluded as a support path while the target exists. Deleting the target changes no watched
    directory, but fresh discovery now finds bar (is_skill_support_path uses exists())."""
    root = tmp_path / 'skills'
    target = tmp_path / 'outside.md'
    target.write_text('---\nname: foo\ndescription: d\n---\n\n# x\n', encoding='utf-8')
    foo = root / 'c' / 'foo'
    foo.mkdir(parents=True)
    os.symlink(str(target), str(foo / 'SKILL.md'))
    _mk(root, 'c/foo/references/bar')
    with ExitStack() as stack:
        env(stack, tmp_path, root)
        assert fresh('bar') is None
        lookup('bar')  # warm: cached miss
        target.unlink()
        assert fresh('bar') is not None, 'model: target removal did not change fresh discovery'
        assert_agrees_with_fresh('bar')


def test_r6_symlinked_skill_md_target_removed_changes_duplicate_winner(tmp_path):
    """Duplicate bare name: the fresh winner's SKILL.md is a symlink to an outside file. Removing
    that file changes no watched directory. On interpreters whose rglob checks existence (3.11),
    fresh discovery now picks the other copy."""
    root = tmp_path / 'skills'
    _mk(root, 'a/foo'), _mk(root, 'b/foo')
    with ExitStack() as stack:
        env(stack, tmp_path, root)
        winner = fresh('foo')
        target = tmp_path / 'outside.md'
        target.write_text((winner / 'SKILL.md').read_text(encoding='utf-8-sig'), encoding='utf-8')
        (winner / 'SKILL.md').unlink()
        os.symlink(str(target), str(winner / 'SKILL.md'))
        assert fresh('foo') == winner
        lookup('foo')
        target.unlink()
        if fresh('foo') == winner:
            pytest.skip('this interpreter\'s rglob counts a dangling SKILL.md (3.12+): no divergence to probe')
        assert_agrees_with_fresh('foo')


def test_r6_dangling_skill_md_target_appears(tmp_path):
    """Reverse: a dangling SKILL.md symlink gains its target outside the tree (3.11 semantics:
    the skill becomes discoverable and wins precedence)."""
    root = tmp_path / 'skills'
    _mk(root, 'a/foo'), _mk(root, 'b/foo')
    with ExitStack() as stack:
        env(stack, tmp_path, root)
        winner = fresh('foo')
        target = tmp_path / 'outside.md'
        content = (winner / 'SKILL.md').read_text(encoding='utf-8-sig')
        (winner / 'SKILL.md').unlink()
        os.symlink(str(target), str(winner / 'SKILL.md'))
        before = fresh('foo')
        lookup('foo')
        target.write_text(content, encoding='utf-8')
        if fresh('foo') == before:
            pytest.skip('this interpreter\'s rglob counts a dangling SKILL.md (3.12+): no divergence to probe')
        assert_agrees_with_fresh('foo')


def test_r6_winner_dir_loses_search_permission(tmp_path):
    """chmod a-x on the winning skill dir changes only that dir's ctime; its stamp is in the index
    but the warm path compares stamps by (dev, ino) only when they differ AND then re-lists shape,
    which scandir still reads (r bit kept). Fresh rglob no longer sees SKILL.md (3.11/3.13)."""
    _need_non_root()
    root = tmp_path / 'skills'
    _mk(root, 'a/foo'), _mk(root, 'b/foo')
    with ExitStack() as stack:
        env(stack, tmp_path, root)
        winner = fresh('foo')
        lookup('foo')
        os.chmod(winner, 0o644)
        try:
            if fresh('foo') == winner:
                pytest.skip('this interpreter\'s rglob still lists SKILL.md in an r-only dir')
            assert_agrees_with_fresh('foo')
        finally:
            os.chmod(winner, 0o755)


def test_r6_winner_dir_regains_search_permission(tmp_path):
    """Reverse: the higher-precedence copy is r-only (invisible to fresh rglob on 3.11/3.13) at
    warm time; chmod +x restores it. The head's shape for that dir is identical before and after
    (scandir needs only r), so a stale loser keeps being edited."""
    _need_non_root()
    root = tmp_path / 'skills'
    _mk(root, 'a/foo'), _mk(root, 'b/foo')
    with ExitStack() as stack:
        env(stack, tmp_path, root)
        winner = fresh('foo')
        os.chmod(winner, 0o644)
        try:
            if fresh('foo') == winner:
                pytest.skip('this interpreter\'s rglob lists SKILL.md in an r-only dir')
            lookup('foo')
            os.chmod(winner, 0o755)
            assert fresh('foo') == winner
            assert_agrees_with_fresh('foo')
        finally:
            os.chmod(winner, 0o755)


def test_r6_symlinked_category_replaced_by_real_dir(tmp_path):
    """A category that is a symlink is not followed by rglob (miss). Replacing the link by the
    real directory (rename into place) is a parent-dir change: must be seen."""
    root = tmp_path / 'skills'
    outside = tmp_path / 'outside-cat'
    _mk(outside, 'foo')
    root.mkdir()
    os.symlink(str(outside), str(root / 'cat'))
    with ExitStack() as stack:
        env(stack, tmp_path, root)
        assert fresh('foo') is None
        lookup('foo')
        (root / 'cat').unlink()
        outside.rename(root / 'cat')
        assert fresh('foo') is not None
        assert_agrees_with_fresh('foo')


def test_r6_real_category_replaced_by_symlink_to_itself(tmp_path):
    """Reverse of the above: a real category dir is moved out and a symlink to it put in its
    place. Fresh rglob does not follow the link (foo vanishes); the head's stamp/shape of 'cat'
    follow the link and are unchanged except ctime (same ino), so the cached hit survives and
    _skill_dir_still_valid(cat/foo) reads through the link."""
    root = tmp_path / 'skills'
    _mk(root, 'cat/foo')
    outside = tmp_path / 'outside-cat'
    with ExitStack() as stack:
        env(stack, tmp_path, root)
        assert fresh('foo') is not None
        lookup('foo')
        (root / 'cat').rename(outside)
        os.symlink(str(outside), str(root / 'cat'))
        assert fresh('foo') is None, 'model: rglob followed the symlink'
        assert_agrees_with_fresh('foo')


def test_r6_symlinked_category_retargeted(tmp_path):
    """A symlinked category retargeted to another real dir: rglob ignores both targets; the
    head stamps the link's TARGET, so this is a pure no-op for both (control for the two above)."""
    root = tmp_path / 'skills'
    t1, t2 = tmp_path / 't1', tmp_path / 't2'
    _mk(t1, 'foo'), _mk(t2, 'bar')
    root.mkdir()
    os.symlink(str(t1), str(root / 'cat'))
    with ExitStack() as stack:
        env(stack, tmp_path, root)
        lookup('foo'), lookup('bar')
        (root / 'cat').unlink()
        os.symlink(str(t2), str(root / 'cat'))
        assert_agrees_with_fresh('bar')
        assert_agrees_with_fresh('foo')


def test_r6_interpreter_version():
    import sys, platform
    print(f'\nR6 python={sys.version.split()[0]} platform={platform.platform()}')


def resolved_by_public_write(name, tag):
    """Which directory does the PUBLIC call resolve ``name`` to? write_file a marker into
    references/ and report where it landed (None on error). Never raises."""
    try:
        r = json.loads(smt.skill_manage(action='write_file', name=name,
                                        file_path=f'references/{tag}.md', file_content=tag))
    except Exception as exc:  # noqa: BLE001
        return ('raised', type(exc).__name__)
    return r.get('success'), r.get('error')


def test_r6_skill_md_replaced_by_directory_and_back(tmp_path):
    """SKILL.md replaced by a DIRECTORY named SKILL.md: rglob still matches the name, so main
    keeps resolving that copy. Compare where head and main resolve via write_file."""
    root = tmp_path / 'skills'
    _mk(root, 'a/foo'), _mk(root, 'b/foo')
    with ExitStack() as stack:
        env(stack, tmp_path, root)
        winner = fresh('foo')
        lookup('foo')
        content = (winner / 'SKILL.md').read_text(encoding='utf-8-sig')
        (winner / 'SKILL.md').unlink()
        (winner / 'SKILL.md').mkdir()
        expected = fresh('foo')
        outcome = resolved_by_public_write('foo', 't1')
        landed = sorted(str(p.relative_to(root)) for p in root.rglob('t1.md'))
        print(f'\nR6 skillmd-as-dir: fresh={expected.relative_to(root)} outcome={outcome} landed={landed}')
        assert landed == [str((expected / 'references' / 't1.md').relative_to(root))] or outcome[0] is not True, landed
        (winner / 'SKILL.md').rmdir()
        (winner / 'SKILL.md').write_text(content, encoding='utf-8')
        assert_agrees_with_fresh('foo')


def test_r6_lowercase_skill_md_on_case_insensitive_fs(tmp_path):
    """macOS/Windows default filesystems are case-insensitive and pathlib.rglob('SKILL.md') then
    matches 'skill.md' (measured on the Studio for py3.11/3.13/3.14). _directory_shape's skill
    flag is an exact NAME match, so _skill_dir_still_valid() rejects every hit on such a skill,
    the lookup loop drops the index 3 times and raises. Main: the skill resolves and patches."""
    root = tmp_path / 'skills'
    d = _mk(root, 'c/lower')
    (d / 'SKILL.md').rename(d / 'skill.md')
    if not (d / 'SKILL.md').exists():
        pytest.skip('case-sensitive filesystem: skill.md is not SKILL.md here')
    with ExitStack() as stack:
        env(stack, tmp_path, root)
        if fresh('lower') is None:
            pytest.skip("this interpreter's rglob is case-sensitive on this filesystem")
        assert_agrees_with_fresh('lower')


def test_r6_skill_md_case_only_rename_on_case_insensitive_fs(tmp_path):
    root = tmp_path / 'skills'
    d = _mk(root, 'c/foo')
    with ExitStack() as stack:
        env(stack, tmp_path, root)
        lookup('foo')
        (d / 'SKILL.md').rename(d / 'skill.md')
        if not (d / 'SKILL.md').exists():
            pytest.skip('case-sensitive filesystem')
        if fresh('foo') is None:
            pytest.skip("this interpreter's rglob is case-sensitive on this filesystem")
        assert_agrees_with_fresh('foo')


def test_r6_coarse_timestamp_model_same_tick_create(tmp_path, impl):
    """MODEL of a coarse-mtime filesystem (HFS+/SMB/NFS/FAT: 1-2 s): directory stamps truncated
    to whole seconds. A skill created in the same second as the warm validation leaves every
    stamp equal, so the head serves the cached miss. Main re-discovers. (On APFS/ext4 with ns
    stamps this is not reproducible; see the 0/2000 measurement in the review.)"""
    root = tmp_path / 'skills'
    _mk(root, 'c/one')
    real = getattr(smt, '_directory_stamp', None)

    def coarse(path):
        st = real(path)
        return None if st is None else (st[0], st[1], st[2] // 10**9 * 10**9, st[3] // 10**9 * 10**9)

    with ExitStack() as stack:
        env(stack, tmp_path, root)
        if _HAS_INDEX:
            stack.enter_context(patch.object(smt, '_directory_stamp', side_effect=coarse))
        lookup('warmup')
        for attempt in range(8):
            time.sleep(1 - (time.time() % 1) + 0.05)
            (root / 'c' / f'touch{attempt}.txt').write_text('x', encoding='utf-8')  # c's mtime is now in THIS second
            lookup(f'new{attempt}')  # warm validation re-lists c and records this second's stamp
            _mk(root, f'c/new{attempt}')
            idx = next(iter(smt._SKILL_INDEX.values()), None) if _HAS_INDEX else None
            if impl == 'head' and _HAS_INDEX:
                if idx is None:
                    continue
                diffs = [(str(p.relative_to(root)) if p != root else '.', st, coarse(p)) for p, (st, _sh) in idx['directories'].items() if coarse(p) != st]
                print(f'\nR6 coarse attempt={attempt} stamp-diffs={diffs}')
                if diffs:
                    continue  # the create crossed a second boundary: not the modelled case
            assert_agrees_with_fresh(f'new{attempt}')
            return
        pytest.skip('could not land warm + create in one second')


def test_r6_unstable_enumeration_order_model(tmp_path):
    """MODEL: a filesystem whose readdir order varies between listings of an UNCHANGED directory
    (some network/FUSE filesystems). No duplicate names exist, so precedence cannot change and
    main's answer is identical every time. The head compares child names in enumeration order:
    a validator/certifier listing that differs from the builder's fails certification."""
    import random
    root = tmp_path / 'skills'
    for i in range(10):
        _mk(root, f'c/s{i}')
    real_scandir = os.scandir
    rng = random.Random(171)

    class Entries:
        def __init__(self, entries): self.entries = iter(entries)
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def __iter__(self): return self
        def __next__(self): return next(self.entries)
        def close(self): pass

    def shuffled(path='.'):
        with real_scandir(path) as entries:
            values = list(entries)
        rng.shuffle(values)
        return Entries(values)

    errors = []
    with ExitStack() as stack:
        env(stack, tmp_path, root)
        stack.enter_context(patch('os.scandir', side_effect=shuffled))
        for i in range(10):
            r = public_patch(f's{i}')  # distinct names: each patch is a first patch
            if not r.get('success'):
                errors.append(r.get('error'))
    print(f'\nR6 unstable-order: errors={len(errors)}/10 sample={errors[:1]}')
    assert not errors, errors[:1]


def test_r6_batch_path_surfaces_index_error_as_exception(tmp_path, impl):
    """The batch shape (operations=[...]) bypasses _bind_skill_targets' except clause, and
    _snapshot_skills calls _find_skill directly: a _SkillIndexChangedError escapes skill_manage as
    an exception instead of a tool_error JSON. Forced by making _build_skill_index never certify."""
    if impl != 'head' or not _HAS_INDEX:
        pytest.skip('head-only error path (no index, no _SkillIndexChangedError)')
    root = tmp_path / 'skills'
    _mk(root, 'c/foo')
    with ExitStack() as stack:
        env(stack, tmp_path, root)
        stack.enter_context(patch.object(smt, '_index_tree_unchanged', return_value=False))
        flat = json.loads(smt.skill_manage(action='patch', name='foo', old_string='# x', new_string='# y'))
        assert flat.get('success') is False and 'retry' in flat.get('error', ''), flat
        try:
            out = smt.skill_manage(action='patch', name='foo', operations=[
                {'action': 'patch', 'name': 'foo', 'old_string': '# x', 'new_string': '# y'}])
        except smt._SkillIndexChangedError as exc:
            pytest.fail(f'batch path raised instead of returning a tool error: {exc!r}')
        assert json.loads(out).get('success') is False


def test_r6_case_only_rename(tmp_path):
    root = tmp_path / 'skills'
    _mk(root, 'c/Foo')
    with ExitStack() as stack:
        env(stack, tmp_path, root)
        lookup('Foo'), lookup('foo')
        (root / 'c' / 'Foo').rename(root / 'c' / 'foo')
        assert_agrees_with_fresh('foo')
        assert_agrees_with_fresh('Foo')


def test_r6_missing_external_root_appears_later(tmp_path):
    local, ext = tmp_path / 'skills', tmp_path / 'ext'
    _mk(local, 'c/other')
    with ExitStack() as stack:
        stack.enter_context(patch.object(smt, 'HERMES_HOME', tmp_path))
        (local / '.locks').mkdir(exist_ok=True)
        stack.enter_context(_roots(local, ext))
        for symbol in ('_apply_skill_write_gate', '_background_review_preflight', '_record_success'):
            stack.enter_context(patch.object(smt, symbol, return_value=None))
        stack.enter_context(patch.object(smt, '_find_skill_in_other_profiles', return_value=[]))
        lookup('foo')
        _mk(ext, 'c/foo')
        assert_agrees_with_fresh('foo')


def test_r6_plain_dir_under_skill_gains_skill_md(tmp_path):
    """Nested skill: c/outer/inner exists without SKILL.md, then gains one."""
    root = tmp_path / 'skills'
    _mk(root, 'c/outer')
    (root / 'c' / 'outer' / 'inner').mkdir()
    with ExitStack() as stack:
        env(stack, tmp_path, root)
        lookup('inner')
        _mk(root, 'c/outer/inner')
        assert_agrees_with_fresh('inner')


def test_r6_mutation_inside_listing_window_same_stamp(tmp_path, impl):
    """Coarse/equal directory timestamps: a child created right after the validator re-lists a
    changed directory, inside the same timestamp tick, leaves the stamp the validator just
    recorded unchanged, so the next lookup trusts the old listing.
    Deterministic on filesystems whose dir mtime has coarse granularity (ext4 on many kernels
    ticks at jiffy/ms resolution; HFS+, FAT, SMB at 1-2 s). The probe records how many attempts
    actually produced an equal stamp, and asserts on those only."""
    if impl != 'head' or not _HAS_INDEX:
        pytest.skip('hook models a mutation inside the head validator window; main has no window')
    root = tmp_path / 'skills'
    for i in range(3):
        _mk(root, f'c/s{i}')
    cat = root / 'c'
    real_shape = smt._directory_shape
    state = {'arm': None}

    def hooked(path):
        out = real_shape(path)
        name = state['arm']
        if name is not None and Path(path) == cat:
            state['arm'] = None
            _mk(root, f'c/{name}')  # lands right after the listing the validator certifies
        return out

    collided, stale = 0, []
    with ExitStack() as stack:
        env(stack, tmp_path, root)
        stack.enter_context(patch.object(smt, '_directory_shape', side_effect=hooked))
        for attempt in range(40):
            name = f'late{attempt}'
            lookup('s0')  # warm and validated
            (cat / f'bump{attempt}.txt').write_text('x', encoding='utf-8')  # changes cat's stamp -> validator re-lists
            st_before = os.stat(cat)
            state['arm'] = name
            lookup('s0')  # validator: stamp differs -> re-list (hook mutates after) -> record stamp
            st_after = os.stat(cat)
            if (st_before.st_mtime_ns, st_before.st_ctime_ns) == (st_after.st_mtime_ns, st_after.st_ctime_ns):
                collided += 1
                got = lookup(name)
                if fresh(name) is not None and 'not found' in json.dumps(got):
                    stale.append(name)
    print(f'\nR6 same-tick: attempts=40 collided={collided} stale_misses={len(stale)} {stale[:5]}')
    assert not stale, f'{len(stale)}/{collided} same-tick creations stayed invisible: {stale[:5]}'


# ---------------------------------------------------------------- (B) error paths / availability

def _populate(root, cats, per):
    for c in range(cats):
        for s in range(per):
            d = _mk(root, f'cat{c}/sk{c}-{s}')
            (d / 'references').mkdir()
            (d / 'references' / 'r.md').write_text('r', encoding='utf-8')


@pytest.mark.parametrize('cold', [False, True], ids=['warm-only', 'with-cold-builds'])
def test_r6_one_churning_subdir_fails_unrelated_lookups(tmp_path, cold):
    """A single skill with a scratch subdirectory whose children churn (a run/cache/output dir;
    any non-support, non-excluded child is watched) must not make lookups of OTHER skills fail.
    Main: rglob never certifies, so it cannot fail. Head: every build re-certifies every watched
    directory; a listing that differs fails certification, 3 attempts, then
    _SkillIndexChangedError -> success:false for an unrelated skill."""
    root = tmp_path / 'skills'
    _populate(root, 10, 40)
    busy = _mk(root, 'cat0/busy')
    work = busy / 'runs'
    work.mkdir()
    stop = threading.Event()

    def churn():
        i = 0
        while not stop.is_set():
            p = work / f'r{i % 4}'
            try:
                p.mkdir()
                p.rmdir()
            except OSError:
                pass
            i += 1

    t = threading.Thread(target=churn, daemon=True)
    errors, ok = [], 0
    with ExitStack() as stack:
        env(stack, tmp_path, root)
        lookup('sk1-0')
        t.start()
        try:
            for i in range(12):
                if cold and i % 3 == 0 and _HAS_INDEX:
                    smt._invalidate_skill_index()  # force some cold builds too
                result = public_patch(f'sk{1 + i % 9}-{i}')
                if result.get('success'):
                    ok += 1
                else:
                    errors.append(result.get('error'))
        finally:
            stop.set()
            t.join()
    print(f'\nR6 churn cold={cold}: ok={ok} errors={len(errors)} sample={errors[:1]}')
    assert not errors, f'{len(errors)}/12 unrelated patches failed: {errors[:1]}'


_CREATOR_JOIN_S = 60


def test_r6_concurrent_creates_starve_lookups(tmp_path):
    """Generation fencing: any successful create/delete anywhere (another session, a curator
    batch) bumps the generation and voids an in-flight build. On a large tree with creates landing
    faster than a build, lookups in other threads exhaust 3 attempts and FAIL."""
    root = tmp_path / 'skills'
    _populate(root, 20, 50)
    stop = threading.Event()
    creator_errors = []

    def creator():
        i = 0
        while not stop.is_set():
            r = json.loads(smt.skill_manage(action='create', name=f'tmp-r6-{i}', category='zz',
                                            content='---\nname: tmp-r6-%d\ndescription: d\n---\n\n# x\n' % i))
            if not r.get('success'):
                creator_errors.append(r.get('error'))
            r = json.loads(smt.skill_manage(action='delete', name=f'tmp-r6-{i}'))
            if not r.get('success'):
                creator_errors.append(r.get('error'))
            i += 1

    errors, ok = [], 0
    outlived_deadline = False
    with ExitStack() as stack:
        env(stack, tmp_path, root)
        lookup('sk0-0')
        t = threading.Thread(target=creator, daemon=True)
        t.start()
        try:
            for i in range(10):
                r = public_patch(f'sk{i}-{i}')
                ok += bool(r.get('success'))
                if not r.get('success'):
                    errors.append(r.get('error'))
        finally:
            stop.set()
            t.join(timeout=_CREATOR_JOIN_S)
            outlived_deadline = t.is_alive()
            # Keep the roots/home patches in place until the worker is gone, so a late
            # create/delete never mutates a tree outside this test's isolation.
            t.join()
    assert not outlived_deadline, f'creator thread outlived the {_CREATOR_JOIN_S}s join deadline'
    print(f'\nR6 create-churn: ok={ok} errors={len(errors)} creator_errors={len(creator_errors)} '
          f'sample={(errors or creator_errors)[:1]}')
    assert not errors and not creator_errors, (errors[:1], creator_errors[:1])


# ---------------------------------------------------------------- (C) cost, by call count

class _Counts:
    def __init__(self):
        self.c = Counter()

    def __enter__(self):
        self.stack = ExitStack()
        import pathlib
        for mod, attr in [(os, 'stat'), (os, 'lstat'), (os, 'scandir'), (os, 'walk'),
                          (os, 'listdir'), (pathlib.Path, 'rglob')]:
            real = getattr(mod, attr)
            real = getattr(real, '__wrapped__', real)  # bypass tests/home_io_guard: count real calls only

            def make(real, key):
                def wrapper(*a, **k):
                    self.c[key] += 1
                    return real(*a, **k)
                return wrapper
            self.stack.enter_context(patch.object(mod, attr, make(real, attr)))
        real_iter = smt._iter_skill_dirs
        self.stack.enter_context(patch.object(smt, '_iter_skill_dirs',
                                              side_effect=lambda r: (self.c.update(['_iter_skill_dirs']), real_iter(r))[1]))
        return self.c

    def __exit__(self, *a):
        self.stack.close()


def test_r6_cost_counts_2000_skills(tmp_path, impl):
    root = tmp_path / 'skills'
    _populate(root, 40, 50)  # 2,000 skills, each with a references/ dir; ~4,041 directories
    ndirs = sum(1 for _ in os.walk(root))
    with ExitStack() as stack:
        env(stack, tmp_path, root)
        assert public_patch('sk3-7').get('success')  # cold build + first patch
        lookup('nope-warm')
        with _Counts() as hit:
            r = public_patch('sk17-21')
        assert r.get('success'), r
        with _Counts() as miss:
            r = public_patch('does-not-exist')
        assert not r.get('success')
        unwrap = ExitStack()
        import pathlib
        for mod, attr in [(os, 'stat'), (os, 'lstat'), (os, 'scandir'), (os, 'listdir')]:
            f = getattr(mod, attr)
            unwrap.enter_context(patch.object(mod, attr, getattr(f, '__wrapped__', f)))
        with unwrap:
            t0 = time.perf_counter()
            for i in range(5):
                public_patch(f'sk{20 + i}-{i}')
            dt_hit = (time.perf_counter() - t0) / 5
            t0 = time.perf_counter()
            for i in range(5):
                public_patch(f'missing-{i}')
            dt_miss = (time.perf_counter() - t0) / 5
    print(f'\nR6 cost impl={impl} dirs={ndirs} warm-hit={dict(hit)} warm-miss={dict(miss)} '
          f'wall(guard unwrapped) hit={dt_hit * 1000:.1f}ms miss={dt_miss * 1000:.1f}ms')
