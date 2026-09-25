"""A worker's stale HERMES_KANBAN_WORKSPACES_ROOT pin must not HOLD completion.

Setting ``kanban.workspaces_root`` while workers run leaves each in-flight
worker with the pre-change dispatcher pin. The survivor path resolved its
excluded roots through the fail-closed placement resolver, which raised on the
disagreement -> ``survivor_unavailable`` -> workspace HELD, completion refused.
"""
import argparse
import subprocess
from pathlib import Path

import pytest
import yaml

from hermes_cli import kanban_db as kb


@pytest.fixture
def home(tmp_path, monkeypatch):
    for key in tuple(__import__('os').environ):
        if key.startswith('HERMES_KANBAN_'):
            monkeypatch.delenv(key)
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setenv('HERMES_KANBAN_HOME', str(tmp_path))
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    root = tmp_path / 'ramroot'
    root.mkdir()
    (tmp_path / 'config.yaml').write_text(yaml.safe_dump({
        'kanban': {'workspaces_root': str(root), 'workspaces_root_require_mount': False},
    }))
    kb.init_db()
    return tmp_path


def _git(*args):
    subprocess.run(['git', *args], check=True, capture_output=True)


def _running_task_with_local_origin(home):
    origin = home / 'origin.git'
    _git('init', '-q', '--bare', str(origin))
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title='repro', assignee='default', body='b')
        ws = home / 'kanban' / 'workspaces' / tid
        ws.mkdir(parents=True)
        _git('init', '-q', str(ws))
        (ws / 'f.txt').write_text('x')
        _git('-C', str(ws), 'add', '.')
        _git('-C', str(ws), '-c', 'user.email=a@b', '-c', 'user.name=a', 'commit', '-qm', 'x')
        _git('-C', str(ws), 'remote', 'add', 'origin', origin.as_uri())
        conn.execute("UPDATE tasks SET status='running', workspace_path=? WHERE id=?",
                     (str(ws), tid))
        conn.commit()
    return tid


@pytest.mark.parametrize('pin', ['stale', 'matching'])
def test_completion_with_pin_vs_configured_root(home, monkeypatch, pin):
    tid = _running_task_with_local_origin(home)
    value = home / 'old-root' if pin == 'stale' else home / 'ramroot' / 'default'
    monkeypatch.setenv('HERMES_KANBAN_WORKSPACES_ROOT', str(value))
    if pin == 'stale':
        # Placement must stay fail-closed on the disagreement.
        with pytest.raises(ValueError, match='board pin disagrees with config'):
            kb.workspaces_root()
    with kb.connect_closing() as conn:
        assert kb.complete_task(conn, tid, result='done', summary='done',
                                metadata={'changed_files': ['f.txt']})
        status = conn.execute('SELECT status FROM tasks WHERE id=?', (tid,)).fetchone()[0]
        kinds = [r[0] for r in conn.execute(
            'SELECT kind FROM task_events WHERE task_id=?', (tid,))]
    assert status == 'done'
    assert 'workspace_held' not in kinds


def test_candidates_are_union_and_never_raise(home, monkeypatch):
    monkeypatch.setenv('HERMES_KANBAN_WORKSPACES_ROOT', str(home / 'old-root'))
    roots = kb.workspace_root_candidates()
    assert home / 'old-root' in roots
    assert home / 'ramroot' / 'default' in roots
    assert home / 'kanban' / 'workspaces' in roots


def test_gc_from_stale_pinned_shell_does_not_raise(home, monkeypatch, capsys):
    from hermes_cli import kanban
    monkeypatch.setenv('HERMES_KANBAN_WORKSPACES_ROOT', str(home / 'old-root'))
    assert kanban._cmd_gc(argparse.Namespace(dry_run=True)) == 0
