"""Mount-root admission is spelling-blind (t_800d50d2).

``Path.resolve()`` keeps case, NFC/NFD and the macOS ``/System/Volumes/Data``
firmlink spelling. Before the fix, a task whose ``workspace_path`` named a
protected mount root in another spelling matched no root, so
``_validate_workspace_admission`` returned None and the dispatcher admitted it
as an unprotected custom path with no mount validation (fail-open).
"""
import os
import unicodedata
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_workspace_policy as policy

FIRMLINK = Path('/System/Volumes/Data')


@pytest.fixture
def home(tmp_path, monkeypatch):
    for key in tuple(os.environ):
        if key.startswith('HERMES_KANBAN_'):
            monkeypatch.delenv(key)
    tmp_path = tmp_path.resolve()
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setenv('HERMES_KANBAN_HOME', str(tmp_path))
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    (tmp_path / 'config.yaml').write_text('kanban: {}\n')
    kb.init_db()
    return tmp_path


@pytest.fixture
def layout(home):
    # NFC umlaut so the NFD axis has something to decompose.
    root = home / 'volume' / 'kanban-wörkspaces'
    path = root / 'default' / 't_probe'
    path.mkdir(parents=True)
    with kb.connect_closing() as conn:
        conn.execute(
            'INSERT INTO workspace_mount_roots(root, mount_path) VALUES (?, ?)',
            (str(root), str(root.parent)),
        )
    return root, path


def spell(path: Path, axis: str) -> str:
    text = str(path)
    if axis == 'canonical':
        return text
    if axis == 'case':
        return text.replace('/volume/', '/VOLUME/').replace('kanban-wörk', 'KANBAN-WÖRK')
    if axis == 'nfd':
        return unicodedata.normalize('NFD', text)
    if axis == 'firmlink':
        if not (FIRMLINK / path.relative_to('/')).exists():
            pytest.skip('no /System/Volumes/Data firmlink on this host')
        return str(FIRMLINK / path.relative_to('/'))
    raise AssertionError(axis)


def task(workspace_path, kind='dir'):
    return SimpleNamespace(id='t_probe', workspace_kind=kind, workspace_path=workspace_path)


def mount_gone(monkeypatch):
    monkeypatch.setattr('os.path.ismount', lambda p: False)


def mount_present(monkeypatch, root):
    monkeypatch.setattr('os.path.ismount', lambda p: Path(p) == root.parent)


AXES = ['canonical', 'case', 'nfd', 'firmlink']


@pytest.mark.parametrize('axis', AXES)
def test_unmounted_root_refuses_every_spelling(layout, monkeypatch, axis):
    """Canonical control + one arm per spelling axis: all must fail closed."""
    root, path = layout
    spelled = spell(path, axis)
    mount_gone(monkeypatch)
    with pytest.raises(policy.WorkspaceUnavailable, match='workspaces_root_unmounted'):
        kb._validate_workspace_admission(task(spelled))


@pytest.mark.parametrize('axis', AXES)
def test_mounted_root_admits_every_spelling_under_the_recorded_root(layout, monkeypatch, axis):
    """No spurious refusal: an aliased spelling is admitted AS the protected root."""
    root, path = layout
    spelled = spell(path, axis)
    if axis != 'canonical' and not Path(spelled).is_dir():
        pytest.skip('case/Unicode-sensitive filesystem: the alias names no directory')
    mount_present(monkeypatch, root)
    admission = kb._validate_workspace_admission(task(spelled))
    assert admission is not None
    assert admission.root == root
    assert admission.mount_path == root.parent


@pytest.mark.parametrize('axis', AXES)
def test_physically_vanished_root_refuses_every_spelling(layout, home, monkeypatch, axis):
    """Real mount loss: neither root nor path exists, so there is no filesystem
    identity to compare. The spelling fold alone must still match the row."""
    root, path = layout
    spelled = spell(path, axis)  # decide the firmlink skip while the path exists
    root.parent.rename(home / 'detached')
    mount_gone(monkeypatch)
    with pytest.raises(policy.WorkspaceUnavailable, match='workspaces_root_unmounted'):
        kb._validate_workspace_admission(task(spelled))


@pytest.mark.parametrize('axis', ['case', 'nfd'])
def test_missing_aliased_workspace_is_stranded_not_recreated(layout, monkeypatch, axis):
    """Lost path (no filesystem identity) still matches its root by spelling fold."""
    root, path = layout
    lost = root / 'default' / 't_gone'
    mount_present(monkeypatch, root)
    with pytest.raises(policy.WorkspaceUnavailable, match='stranded_by_mount_loss'):
        kb._validate_workspace_admission(task(spell(lost, axis)))
    assert not lost.exists()


def test_symlink_alias_of_root_is_still_an_escape(layout, home, monkeypatch):
    """Spelling-blindness must not turn a symlinked alias into an admission."""
    root, path = layout
    alias = home / 'alias'
    alias.symlink_to(root, target_is_directory=True)
    mount_present(monkeypatch, root)
    with pytest.raises(policy.WorkspaceUnavailable, match='symlink escape'):
        kb._validate_workspace_admission(task(str(alias / 'default' / 't_probe')))


def test_symlink_inside_aliased_spelling_is_an_escape(layout, home, monkeypatch):
    root, path = layout
    outside = home / 'outside'
    outside.mkdir()
    (root / 'default' / 'link').symlink_to(outside, target_is_directory=True)
    mount_present(monkeypatch, root)
    spelled = unicodedata.normalize('NFD', str(root / 'default' / 'link'))
    with pytest.raises(policy.WorkspaceUnavailable, match='symlink escape'):
        kb._validate_workspace_admission(task(spelled))


def test_configured_root_alias_of_recorded_row_fails_closed(layout, home, monkeypatch):
    """A config root spelled differently must not drop the recorded mount anchor."""
    root, _path = layout
    (home / 'config.yaml').write_text(yaml.safe_dump({'kanban': {
        'workspaces_root': unicodedata.normalize('NFD', str(root)),
        'workspaces_root_require_mount': True,
    }}))
    mount_present(monkeypatch, root)
    with pytest.raises(policy.WorkspaceUnavailable, match='alias of recorded root'):
        kb._validate_workspace_admission(task(None, kind='scratch'))
    with kb.connect_closing() as conn:
        rows = conn.execute('SELECT count(*) FROM workspace_mount_roots').fetchone()[0]
    assert rows == 1


@pytest.mark.parametrize('axis', ['case', 'nfd'])
def test_dispatcher_strands_aliased_task_instead_of_spawning(layout, monkeypatch, axis):
    """End to end: the dispatcher must not spawn onto an unmounted aliased root."""
    root, path = layout
    mount_gone(monkeypatch)
    with kb.connect_closing() as conn:
        task_id = kb.create_task(
            conn, title='aliased', assignee='default',
            workspace_kind='dir', workspace_path=spell(path, axis),
        )
        calls = []
        result = kb.dispatch_once(conn, spawn_fn=lambda *a, **k: calls.append(a))
        assert not calls
        assert task_id in result.stranded_by_mount_loss
