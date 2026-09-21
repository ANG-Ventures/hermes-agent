"""Mount-loss admission at the real workspace/dispatcher boundary."""
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_db as kb


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


def configure(home, root, require_mount=True):
    (home / 'config.yaml').write_text(
        f'kanban:\n  workspaces_root: {root}\n'
        f'  workspaces_root_require_mount: {str(require_mount).lower()}\n'
    )


def scratch(path=None):
    return SimpleNamespace(id='t_probe', workspace_kind='scratch', workspace_path=path)


def test_config_preserves_board_isolation(home):
    root = home / 'scratch'
    configure(home, root, False)
    assert kb.workspaces_root('default') == root / 'default'
    assert kb.workspaces_root('other') == root / 'other'


@pytest.mark.parametrize('precreate', [False, True])
def test_missing_mount_never_creates_workspace(home, precreate):
    root = home / 'absent-volume' / 'kanban-workspaces'
    if precreate:
        root.mkdir(parents=True)
    configure(home, root)
    with pytest.raises(ValueError, match='workspaces_root_unmounted'):
        kb.resolve_workspace(scratch(), board='default')
    assert not (root / 'default').exists()
    assert root.exists() == precreate


def test_persisted_path_missing_after_config_rollback_is_not_recreated(home):
    path = home / 'gone-volume' / 'kanban-workspaces' / 't_probe'
    with pytest.raises(ValueError, match='stranded_by_mount_loss|workspace_missing'):
        kb.resolve_workspace(scratch(str(path)), board='default')
    assert not path.exists()


def test_dispatch_refuses_before_spawn_or_failure_count(home):
    root = home / 'absent-volume' / 'kanban-workspaces'
    configure(home, root)
    with kb.connect() as conn:
        task = kb.create_task(conn, title='mount probe', assignee='default')
        calls = []
        result = kb.dispatch_once(conn, spawn_fn=lambda *args, **kw: calls.append(args))
        assert not calls
        assert not result.spawned
        assert not result.spawn_failed
        row = conn.execute('SELECT consecutive_failures FROM tasks WHERE id=?', (task,)).fetchone()
        assert row[0] == 0
        assert not root.exists()
