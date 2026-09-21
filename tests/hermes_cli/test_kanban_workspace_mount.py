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


def test_filesystem_root_never_satisfies_mount_guard(monkeypatch):
    from hermes_cli import kanban_workspace_policy as policy

    monkeypatch.setattr('os.path.ismount', lambda p: Path(p) == Path('/'))
    with pytest.raises(ValueError, match='workspaces_root_unmounted'):
        policy.validate_mount(Path('/tmp'))
    with pytest.raises(ValueError, match='workspaces_root_invalid'):
        policy.validate_mount(Path('/'))


def test_persisted_path_missing_after_config_rollback_is_not_recreated(home, monkeypatch):
    root = home / 'volume' / 'kanban-workspaces'
    root.mkdir(parents=True)
    configure(home, root)
    monkeypatch.setattr('os.path.ismount', lambda p: Path(p) == root.parent)
    path = kb.resolve_workspace(scratch(), board='default')
    path.rmdir()
    (home / 'config.yaml').write_text('kanban: {}\n')
    monkeypatch.setattr('os.path.ismount', lambda p: False)
    with pytest.raises(ValueError, match='stranded_by_mount_loss|workspaces_root_unmounted'):
        kb.resolve_workspace(scratch(str(path)), board='default')
    assert not path.exists()


def test_durable_explicit_scratch_keeps_creation_contract(home):
    path = home / 'durable' / 'new-workspace'
    assert kb.resolve_workspace(scratch(str(path)), board='default') == path
    assert path.is_dir()


def test_persisted_dir_cannot_bypass_registered_mount(home, monkeypatch):
    root = home / 'volume' / 'kanban-workspaces'
    root.mkdir(parents=True)
    configure(home, root)
    monkeypatch.setattr('os.path.ismount', lambda p: Path(p) == root.parent)
    path = kb.resolve_workspace(scratch(), board='default')
    (home / 'config.yaml').write_text('kanban: {}\n')
    monkeypatch.setattr('os.path.ismount', lambda p: False)
    task = SimpleNamespace(id='t_probe', workspace_kind='dir', workspace_path=str(path))
    with pytest.raises(ValueError, match='workspaces_root_unmounted'):
        kb.resolve_workspace(task, board='default')


@pytest.mark.parametrize('dry_run', [False, True])
def test_dispatch_refuses_before_spawn_or_failure_count(home, dry_run):
    root = home / 'absent-volume' / 'kanban-workspaces'
    configure(home, root)
    with kb.connect() as conn:
        task = kb.create_task(conn, title='mount probe', assignee='default')
        calls = []
        result = kb.dispatch_once(conn, spawn_fn=lambda *args, **kw: calls.append(args), dry_run=dry_run)
        assert not calls
        assert not result.spawned
        assert not result.spawn_failed
        row = conn.execute('SELECT consecutive_failures FROM tasks WHERE id=?', (task,)).fetchone()
        assert row[0] == 0
        assert not root.exists()

@pytest.mark.parametrize('kind', ['scratch', 'dir', 'worktree'])
def test_restart_marks_missing_persisted_workspace_without_recreating(home, monkeypatch, kind):
    root = home / 'volume' / 'kanban-workspaces'
    root.mkdir(parents=True)
    configure(home, root)
    monkeypatch.setattr('os.path.ismount', lambda p: Path(p) == root.parent)
    path = kb.resolve_workspace(scratch(), board='default')
    path.rmdir()
    with kb.connect_closing() as conn:
        task_id = kb.create_task(conn, title='lost workspace', assignee='default',
                                 workspace_kind=kind, workspace_path=str(path))
        calls = []
        result = kb.dispatch_once(conn, spawn_fn=lambda *args, **kw: calls.append(args))
        assert not calls
        assert task_id in result.stranded_by_mount_loss
        assert not path.exists()
        assert conn.execute("SELECT count(*) FROM task_events WHERE task_id=? AND kind='stranded_by_mount_loss'",
                            (task_id,)).fetchone()[0] == 1
        kb.dispatch_once(conn, spawn_fn=lambda *args, **kw: calls.append(args))
        assert conn.execute("SELECT count(*) FROM task_events WHERE task_id=? AND kind='stranded_by_mount_loss'",
                            (task_id,)).fetchone()[0] == 1


def test_board_symlink_cannot_redirect_creation(home, monkeypatch):
    root = home / 'volume' / 'kanban-workspaces'
    root.mkdir(parents=True)
    outside = home / 'outside'
    outside.mkdir()
    (root / 'default').symlink_to(outside, target_is_directory=True)
    configure(home, root)
    monkeypatch.setattr('os.path.ismount', lambda p: Path(p) == root.parent)
    with pytest.raises((OSError, ValueError)):
        kb.resolve_workspace(scratch(), board='default')
    assert not (outside / 't_probe').exists()


def test_unwritable_mount_refuses_before_claim(home, monkeypatch):
    root = home / 'volume' / 'kanban-workspaces'
    root.mkdir(parents=True)
    configure(home, root)
    monkeypatch.setattr('os.path.ismount', lambda p: Path(p) == root.parent)

    from hermes_cli import kanban_workspace_policy as policy

    real_open = policy.os.open

    def refuse_probe(path, flags, *args, **kwargs):
        if str(path).startswith('.hermes-write-probe-') and flags & policy.os.O_CREAT:
            raise PermissionError('read-only mount')
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(policy.os, 'open', refuse_probe)
    with kb.connect_closing() as conn:
        task_id = kb.create_task(conn, title='read-only mount', assignee='default')
        calls = []
        result = kb.dispatch_once(conn, spawn_fn=lambda *args, **kw: calls.append(args))
        assert not calls
        assert task_id in [item[0] for item in result.workspace_refused]
        assert 'workspaces_root_unwritable' in dict(result.workspace_refused)[task_id]
        task = kb.get_task(conn, task_id)
        assert task is not None
        assert task.status == 'ready'
