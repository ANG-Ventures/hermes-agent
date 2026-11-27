"""Apollo round-7 probes for PR 171 at 8d3313e84b: the per-call memo and the per-skill mutation lock.

skill_manage resolves the name once BEFORE it takes the per-skill mutation lock (the
background-review preflight) and again under the lock (ledger pre-capture, then the handler).
On main each of those is a fresh discovery, so the lookups under the lock see every write that
finished while this call was waiting for the lock. With the per-call memo the preflight answer
is reused under the lock.

Each probe runs a REAL second writer in another thread that holds the real per-skill lock,
mutates the tree while the call under test is parked on that lock (after its preflight), then
releases. The oracle is main's lookup (tests/tools/review171_legacy_lookup.py) run at the moment
the lock is handed over. Arms: `head` = the code under test; `main-control` = the same public
call with main's lookup patched in as smt._find_skill.
"""
import json
import shutil
import threading
import types
from unittest.mock import patch

import pytest

from tools import skill_manager_tool as smt
from tests.tools.test_skill_manager_index import _mk, _public_env
from tests.tools import review171_legacy_lookup as legacy

_LEGACY = types.FunctionType(legacy._find_skill.__code__, smt.__dict__)


@pytest.fixture(autouse=True, params=['head', 'main-control'])
def impl(request):
    if request.param == 'head':
        yield 'head'
    else:
        with patch.object(smt, '_find_skill', _LEGACY):
            yield 'main'


def _race(tmp_path, local, ext, name, mutate):
    """Run skill_manage(patch name) in thread X while thread Y holds name's mutation lock.
    Y waits until X has finished its preflight and is parked on the lock, runs mutate(),
    records main's fresh answer, then releases. Returns (result, fresh_path_at_handover)."""
    (local / '.locks').mkdir(parents=True, exist_ok=True)
    real_preflight = smt._background_review_preflight
    holding, x_past_preflight, x_done = threading.Event(), threading.Event(), threading.Event()
    out = {}

    def preflight(action, nm):
        try:
            return real_preflight(action, nm)
        finally:
            if threading.current_thread().name == 'X':
                x_past_preflight.set()

    def writer():
        with smt._skill_mutation_lock(name):
            holding.set()
            assert x_past_preflight.wait(20), 'X never reached the end of its preflight'
            # X is now between its preflight and the lock; give it time to park on the lock.
            out['x_parked'] = not x_done.wait(0.5)
            mutate()
            found = _LEGACY(name)
            out['fresh'] = found['path'] if found else None

    def caller():
        try:
            out['result'] = json.loads(smt.skill_manage(
                action='patch', name=name, old_string='old line', new_string='new line'))
        except BaseException as exc:  # noqa: BLE001 - the probe reports an escape as data
            out['raised'] = repr(exc)
        finally:
            x_done.set()

    with _public_env(tmp_path, local, ext), patch.object(smt, '_background_review_preflight', preflight):
        y = threading.Thread(target=writer, name='Y')
        y.start()
        assert holding.wait(20)
        x = threading.Thread(target=caller, name='X')
        x.start()
        y.join(40)
        x.join(40)
    assert not y.is_alive() and not x.is_alive(), 'probe threads did not finish'
    assert out.get('x_parked'), 'X was not parked on the lock while Y mutated: probe is vacuous'
    assert 'raised' not in out, f"skill_manage raised instead of returning an error: {out['raised']}"
    return out['result'], out['fresh']


def _patched(skill_dir):
    return 'new line' in (skill_dir / 'SKILL.md').read_text(encoding='utf-8-sig')


def test_r7_higher_precedence_copy_created_while_waiting_for_the_lock(tmp_path):
    """Only the external copy exists at preflight. While the call waits for the lock another
    writer creates a local copy (which wins). The patch must land on the copy a discovery
    under the lock finds: the local one."""
    local, ext = tmp_path / 'skills', tmp_path / 'ext'
    old = _mk(ext, 'foo')
    result, fresh = _race(tmp_path, local, ext, 'foo', lambda: _mk(local, 'c/foo'))
    assert fresh == local / 'c' / 'foo'
    assert result.get('success'), result
    assert _patched(fresh) and not _patched(old), (
        f'patch landed on the copy found BEFORE the lock ({old}) instead of the winner under the lock ({fresh})')


def test_r7_winner_deleted_while_waiting_for_the_lock(tmp_path):
    """Local and external copies exist at preflight (local wins). While the call waits for the
    lock another writer deletes the local copy. The patch must land on the external copy."""
    local, ext = tmp_path / 'skills', tmp_path / 'ext'
    gone, survivor = _mk(local, 'c/foo'), _mk(ext, 'foo')
    result, fresh = _race(tmp_path, local, ext, 'foo', lambda: shutil.rmtree(gone))
    assert fresh == survivor
    assert result.get('success'), f'main patches the surviving copy; this call answered: {result}'
    assert _patched(survivor)


def test_r7_only_copy_deleted_while_waiting_for_the_lock(tmp_path):
    """The only copy is deleted while the call waits for the lock: a clean not-found error."""
    local, ext = tmp_path / 'skills', tmp_path / 'ext'
    ext.mkdir(parents=True, exist_ok=True)
    gone = _mk(local, 'c/foo')
    result, fresh = _race(tmp_path, local, ext, 'foo', lambda: shutil.rmtree(gone))
    assert fresh is None
    assert result.get('success') is False, result
    print(f"\nR7 only-copy-deleted error: {str(result.get('error'))[:200]!r}")
    # Main's answer is its skill-not-found error, not a file error from a path that is gone.
    assert "skill 'foo' not found" in str(result.get('error', '')).lower(), result
