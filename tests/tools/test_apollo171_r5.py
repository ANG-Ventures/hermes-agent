"""Operator probes for Prism findings on PR 171 at 8148df0e (round 5): 225ea824b87f / 97e9a72be65e and 6a726bb2ae97."""
import json
import os
from contextlib import ExitStack
from unittest.mock import patch

import pytest

from tools import skill_manager_tool as smt
from tests.tools.test_skill_manager_index import _mk, _roots


@pytest.fixture(autouse=True, params=['head', 'main-rglob-control'])
def discovery_implementation(request):
    if request.param == 'head':
        yield
    else:
        import types
        from tests.tools import review171_legacy_lookup as legacy
        lookup = types.FunctionType(legacy._find_skill.__code__, smt.__dict__)
        with patch.object(smt, '_find_skill', lookup):
            yield


def env(stack, home, root):
    (root / '.locks').mkdir(exist_ok=True)
    stack.enter_context(_roots(root))
    for symbol, value in [('HERMES_HOME', home), ('_apply_skill_write_gate', None),
                          ('_background_review_preflight', None), ('_record_success', None),
                          ('_find_skill_in_other_profiles', [])]:
        stack.enter_context(patch.object(smt, symbol, home) if symbol == 'HERMES_HOME'
                            else patch.object(smt, symbol, return_value=value))


def public(name, action='unknown'):
    return json.loads(smt.skill_manage(action=action, name=name, old_string='# x', new_string='# changed'))


def test_dangling_skill_md_symlink_does_not_break_healthy_skills(tmp_path):
    """6a726bb2ae97: one dangling SKILL.md symlink anywhere must not make unrelated operations fail."""
    root = tmp_path / 'skills'
    healthy = _mk(root, 'cat/healthy')
    broken = root / 'cat' / 'broken'
    broken.mkdir(parents=True)
    os.symlink(str(tmp_path / 'does-not-exist.md'), str(broken / 'SKILL.md'))
    with ExitStack() as stack:
        env(stack, tmp_path, root)
        result = public('healthy', 'patch')
        assert result.get('success'), result
        assert '# changed' in (healthy / 'SKILL.md').read_text(encoding='utf-8-sig')


def test_rename_away_and_back_reorders_duplicate_name_precedence(tmp_path):
    """225ea824b87f / 97e9a72be65e: same inodes, same sorted shape, different enumeration order."""
    root = tmp_path / 'skills'
    _mk(root, 'a/foo')
    _mk(root, 'b/foo')
    flipped = tmp_path / 'flipped'
    real_scandir = os.scandir

    class Entries:
        def __init__(self, entries): self.entries = iter(entries)
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def __iter__(self): return self
        def __next__(self): return next(self.entries)
        def close(self): pass

    def ordered_scandir(path):
        with real_scandir(path) as entries:
            values = list(entries)
        return Entries(sorted(values, key=lambda e: e.name, reverse=flipped.exists()))

    with ExitStack() as stack:
        env(stack, tmp_path, root)
        stack.enter_context(patch('os.scandir', side_effect=ordered_scandir))
        public('foo')  # warm
        original = next(smt._iter_skill_dirs(root)).relative_to(root)
        # Rename the winner's category away and back: inode and sorted child names are unchanged.
        winner_cat = root / original.parts[0]
        winner_cat.rename(tmp_path / 'parked')
        (tmp_path / 'parked').rename(winner_cat)
        flipped.write_text('1', encoding='utf-8')  # the filesystem now enumerates in the other order
        fresh = next(smt._iter_skill_dirs(root))
        assert fresh.relative_to(root) != original, 'model did not change discovery order'
        result = public('foo', 'patch')
        assert result.get('success'), result
        assert '# changed' in (fresh / 'SKILL.md').read_text(encoding='utf-8-sig'), (original, fresh, result)
