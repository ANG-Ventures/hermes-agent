"""A scratch card persisted under a RETIRED workspaces_root is reallocated.

Live shape (t_4b9809f4, 2026-09-26): ``kanban.workspaces_root`` was
/Volumes/ramscratch/kanban-workspaces (mount-guarded), then retired from
config and the volume unmounted. The card kept
``workspace_path=/Volumes/ramscratch/kanban-workspaces/default/t_4b9809f4``,
kind scratch, and the recorded ``workspace_mount_roots`` row refused it as
``workspaces_root_unmounted`` every tick for 2.7h while it read as 'ready'.
"""
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from hermes_cli import kanban as kc
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_db_workspace as kbw


@pytest.fixture
def home(tmp_path, monkeypatch):
    for key in tuple(__import__('os').environ):
        if key.startswith('HERMES_KANBAN_'):
            monkeypatch.delenv(key)
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setenv('HERMES_KANBAN_HOME', str(tmp_path))
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    kb.init_db()
    return tmp_path


def _configure(home, root, require_mount=True):
    kanban = {}
    if root is not None:
        kanban = {'workspaces_root': str(root), 'workspaces_root_require_mount': require_mount}
    (home / 'config.yaml').write_text(yaml.safe_dump({'kanban': kanban}))


def _card_under_retired_root(home, monkeypatch, conn, *, kind='scratch', status='ready'):
    """Persist a card under a mount-guarded root, then retire + unmount it."""
    ramscratch = home / 'Volumes' / 'ramscratch'
    root = ramscratch / 'kanban-workspaces'
    root.mkdir(parents=True)
    _configure(home, root)
    monkeypatch.setattr('os.path.ismount', lambda p: Path(p) == ramscratch)
    task_id = kb.create_task(conn, title='retired root', assignee='default')
    path = kbw.resolve_workspace(
        SimpleNamespace(id=task_id, workspace_kind='scratch', workspace_path=None),
        board='default',
    )
    assert path == root / 'default' / task_id
    kbw.set_workspace_path(conn, task_id, path)
    conn.execute('UPDATE tasks SET workspace_kind=?, status=? WHERE id=?', (kind, status, task_id))
    # Retire: config no longer names ramscratch; the volume goes away.
    _configure(home, None)
    monkeypatch.setattr('os.path.ismount', lambda p: False)
    shutil.rmtree(ramscratch)
    return task_id, path, root


def _events(conn, task_id, kind):
    return [r[0] for r in conn.execute(
        'SELECT payload FROM task_events WHERE task_id=? AND kind=? ORDER BY id',
        (task_id, kind),
    )]


def test_retired_root_scratch_card_is_reallocated_and_spawns_same_tick(home, monkeypatch):
    with kb.connect_closing() as conn:
        task_id, old_path, root = _card_under_retired_root(home, monkeypatch, conn)
        calls = []
        result = kbd.dispatch_once(conn, spawn_fn=lambda task, *_a, **_k: calls.append(task.id))
        assert task_id in calls
        assert task_id not in [t for t, _ in result.workspace_refused]
        assert task_id not in result.stranded_by_mount_loss
        new_path = Path(kb.get_task(conn, task_id).workspace_path)
        assert new_path == kb.workspaces_root('default') / task_id
        assert new_path.is_dir()
        assert not old_path.exists()
        (payload,) = _events(conn, task_id, 'workspace_reallocated')
        assert str(old_path) in payload and str(root) in payload
        assert 'workspaces_root_unmounted' in payload


def test_retired_root_heals_when_old_volume_is_mounted_but_tree_gone(home, monkeypatch):
    with kb.connect_closing() as conn:
        task_id, old_path, root = _card_under_retired_root(home, monkeypatch, conn)
        root.mkdir(parents=True)  # old volume back, card tree gone
        monkeypatch.setattr('os.path.ismount', lambda p: Path(p) == root.parent)
        calls = []
        kbd.dispatch_once(conn, spawn_fn=lambda task, *_a, **_k: calls.append(task.id))
        assert task_id in calls
        assert Path(kb.get_task(conn, task_id).workspace_path).parent == kb.workspaces_root('default')
        assert 'stranded_by_mount_loss' in _events(conn, task_id, 'workspace_reallocated')[0]


@pytest.mark.parametrize('kind', ['dir', 'worktree'])
def test_retired_root_never_moves_operator_owned_paths(home, monkeypatch, kind):
    with kb.connect_closing() as conn:
        task_id, old_path, _root = _card_under_retired_root(home, monkeypatch, conn, kind=kind)
        calls = []
        result = kbd.dispatch_once(conn, spawn_fn=lambda task, *_a, **_k: calls.append(task.id))
        assert not calls
        assert task_id in [t for t, _ in result.workspace_refused]
        assert kb.get_task(conn, task_id).workspace_path == str(old_path)
        assert not _events(conn, task_id, 'workspace_reallocated')


def test_current_root_unmounted_is_not_treated_as_retired(home, monkeypatch):
    # Same shape, but the root is STILL configured: a remount may bring the
    # path back, so this stays a fail-closed refusal.
    with kb.connect_closing() as conn:
        task_id, old_path, root = _card_under_retired_root(home, monkeypatch, conn)
        _configure(home, root)
        calls = []
        result = kbd.dispatch_once(conn, spawn_fn=lambda task, *_a, **_k: calls.append(task.id))
        assert not calls
        assert task_id in [t for t, _ in result.workspace_refused]
        assert kb.get_task(conn, task_id).workspace_path == str(old_path)


def test_retired_root_reallocates_under_a_new_configured_root(home, monkeypatch):
    with kb.connect_closing() as conn:
        task_id, _old, _root = _card_under_retired_root(home, monkeypatch, conn)
        new_root = home / 'Volumes' / 'fast' / 'kanban-workspaces'
        new_root.mkdir(parents=True)
        _configure(home, new_root)
        monkeypatch.setattr('os.path.ismount', lambda p: Path(p) == new_root.parent)
        calls = []
        kbd.dispatch_once(conn, spawn_fn=lambda task, *_a, **_k: calls.append(task.id))
        assert task_id in calls
        assert kb.get_task(conn, task_id).workspace_path == str(new_root / 'default' / task_id)


def test_dry_run_does_not_reallocate(home, monkeypatch):
    with kb.connect_closing() as conn:
        task_id, old_path, _root = _card_under_retired_root(home, monkeypatch, conn)
        kbd.dispatch_once(conn, spawn_fn=lambda *_a, **_k: None, dry_run=True)
        assert kb.get_task(conn, task_id).workspace_path == str(old_path)


def test_refused_card_is_flagged_in_list_and_show(home, monkeypatch, capsys):
    with kb.connect_closing() as conn:
        task_id, _old, _root = _card_under_retired_root(home, monkeypatch, conn, kind='dir')
        kbd.dispatch_once(conn, spawn_fn=lambda *_a, **_k: None)
        state = kb.workspace_refusal_state(conn, [task_id])[task_id]
        assert state['reason'].startswith('workspaces_root_unmounted:')
    monkeypatch.delenv('HERMES_SESSION_ID', raising=False)
    out = kc.run_slash('list')
    line = next(l for l in out.splitlines() if task_id in l)
    assert 'WORKSPACE REFUSED (workspaces_root_unmounted' in line, line
    out = kc.run_slash(f'show {task_id}')
    status = next(l for l in out.splitlines() if l.strip().startswith('status:'))
    assert 'WORKSPACE REFUSED' in status, status


def test_refusal_flag_clears_after_reallocation_and_claim(home, monkeypatch):
    with kb.connect_closing() as conn:
        task_id, _old, _root = _card_under_retired_root(home, monkeypatch, conn)
        kb._append_event(conn, task_id, 'workspace_refused',
                         {'reason': 'workspaces_root_unmounted: /x'})
        assert task_id in kb.workspace_refusal_state(conn, [task_id])
        kbd.dispatch_once(conn, spawn_fn=lambda *_a, **_k: None)
        assert task_id not in kb.workspace_refusal_state(conn, [task_id])
