"""Mount-loss admission at the real workspace/dispatcher boundary."""
from pathlib import Path
from types import SimpleNamespace

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
    kb.init_db()
    return tmp_path


def configure(home, root, require_mount=True):
    (home / 'config.yaml').write_text(
        yaml.safe_dump({
            'kanban': {
                'workspaces_root': str(root),
                'workspaces_root_require_mount': require_mount,
            },
        })
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


def test_recorded_mount_anchor_cannot_fall_back_to_mounted_parent(home, monkeypatch):
    from hermes_cli import kanban_workspace_policy as policy

    root = home / 'mounted-root'
    root.mkdir()
    monkeypatch.setattr(
        'os.path.ismount',
        lambda p: Path(p) in {root, root.parent},
    )
    assert policy.validate_mount(root) == root
    monkeypatch.setattr('os.path.ismount', lambda p: Path(p) == root.parent)
    with pytest.raises(ValueError, match='workspaces_root_unmounted'):
        policy.validate_mount(root, expected_mount=root)


def test_create_scratch_keeps_admitted_mount_anchor(home, monkeypatch):
    from hermes_cli import kanban_workspace_policy as policy

    root = home / 'mounted-root'
    root.mkdir()
    configure(home, root)
    mount_present = True
    monkeypatch.setattr(
        policy.os.path,
        'ismount',
        lambda p: Path(p) == (root if mount_present else root.parent),
    )
    real_validate_target = policy.validate_target

    def vanish_after_admission(admitted_root, target):
        nonlocal mount_present
        real_validate_target(admitted_root, target)
        mount_present = False

    monkeypatch.setattr(policy, 'validate_target', vanish_after_admission)
    target = root / 'default' / 't_probe'
    with pytest.raises(ValueError, match='workspaces_root_unmounted'):
        kb.resolve_workspace(scratch(), board='default')
    assert not target.exists()


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
        assert task in [item[0] for item in result.workspace_refused]
        persisted = kb.get_task(conn, task)
        assert persisted is not None
        assert persisted.status == 'ready'
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
        retry = kb.dispatch_once(conn, spawn_fn=lambda *args, **kw: calls.append(args))
        assert task_id in retry.stranded_by_mount_loss
        assert not calls
        assert not path.exists()
        assert conn.execute("SELECT count(*) FROM task_events WHERE task_id=? AND kind='stranded_by_mount_loss'",
                            (task_id,)).fetchone()[0] == 1


def test_nonspawnable_startup_stranding_is_not_a_ready_lane_fault(home, monkeypatch):
    root = home / 'volume' / 'kanban-workspaces'
    root.mkdir(parents=True)
    configure(home, root)
    monkeypatch.setattr('os.path.ismount', lambda p: Path(p) == root.parent)
    path = kb.resolve_workspace(scratch(), board='default')
    path.rmdir()
    with kb.connect_closing() as conn:
        parent = kb.create_task(conn, title='blocked parent', assignee='default')
        conn.execute("UPDATE tasks SET status='blocked' WHERE id=?", (parent,))
        task_id = kb.create_task(
            conn, title='lost todo', assignee='default',
            workspace_kind='scratch', workspace_path=str(path),
        )
        kb.link_tasks(conn, parent, task_id)
        result = kb.dispatch_once(conn, spawn_fn=lambda *_args, **_kw: None)
        assert task_id in result.stranded_by_mount_loss
        assert task_id not in [item[0] for item in result.workspace_refused]


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

    with kb.connect_closing() as conn:
        task_id = kb.create_task(conn, title='symlink target', assignee='default')
        result = kb.dispatch_once(conn, spawn_fn=lambda *_args, **_kw: None)
        assert task_id in [item[0] for item in result.workspace_refused]
        assert not result.spawn_failed


def test_persisted_workspace_race_is_never_recreated(home, monkeypatch):
    root = home / 'volume' / 'kanban-workspaces'
    root.mkdir(parents=True)
    configure(home, root)
    monkeypatch.setattr('os.path.ismount', lambda p: Path(p) == root.parent)
    path = kb.resolve_workspace(scratch(), board='default')

    from hermes_cli import kanban_workspace_policy as policy

    real_validate = policy.validate_persisted

    def delete_after_validation(candidate):
        real_validate(candidate)
        candidate.rmdir()

    monkeypatch.setattr(policy, 'validate_persisted', delete_after_validation)
    resolved = kb.resolve_workspace(scratch(str(path)), board='default')
    assert resolved == path
    assert not path.exists()


def test_post_claim_mount_race_requeues_without_failure_charge(home, monkeypatch):
    root = home / 'volume' / 'kanban-workspaces'
    root.mkdir(parents=True)
    configure(home, root)
    monkeypatch.setattr('os.path.ismount', lambda p: Path(p) == root.parent)

    from hermes_cli import kanban_workspace_policy as policy

    def vanish_during_create(_root, _path, **_kwargs):
        raise policy.WorkspaceUnavailable(f'workspaces_root_unmounted: {root}')

    monkeypatch.setattr(policy, 'create_scratch', vanish_during_create)
    with kb.connect_closing() as conn:
        task_id = kb.create_task(conn, title='mount race', assignee='default')
        calls = []
        result = kb.dispatch_once(conn, spawn_fn=lambda *args, **kw: calls.append(args))
        task = kb.get_task(conn, task_id)
        assert task is not None
        assert task.status == 'ready'
        assert task.consecutive_failures == 0
        assert not calls
        assert not result.spawn_failed
        assert task_id in [item[0] for item in result.workspace_refused]
        run = conn.execute(
            'SELECT outcome FROM task_runs WHERE task_id=? ORDER BY id DESC LIMIT 1',
            (task_id,),
        ).fetchone()
        assert run['outcome'] == 'workspace_refused'


def test_review_post_claim_mount_race_returns_to_review(home, monkeypatch):
    root = home / 'volume' / 'kanban-workspaces'
    root.mkdir(parents=True)
    configure(home, root)
    monkeypatch.setattr('os.path.ismount', lambda p: Path(p) == root.parent)
    from hermes_cli import kanban_workspace_policy as policy

    with kb.connect_closing() as conn:
        task_id = kb.create_task(conn, title='review mount race', assignee='default')
        claimed = kb.claim_task(conn, task_id)
        assert claimed is not None
        assert kb.request_review(
            conn, task_id, reviewer='default',
            expected_run_id=claimed.current_run_id,
        )

        def vanish_during_create(_root, _path, **_kwargs):
            raise policy.WorkspaceUnavailable(f'workspaces_root_unmounted: {root}')

        monkeypatch.setattr(policy, 'create_scratch', vanish_during_create)
        result = kb.dispatch_once(conn, spawn_fn=lambda *_args, **_kw: None)
        task = kb.get_task(conn, task_id)
        assert task is not None
        assert task.status == 'review'
        assert task.current_run_id is None
        assert task.consecutive_failures == 0
        assert not result.spawn_failed
        assert task_id in [item[0] for item in result.workspace_refused]


def test_most_specific_historical_root_wins(home, monkeypatch):
    broad = home / 'volume'
    specific = broad / 'kanban-workspaces'
    path = specific / 'default' / 't_probe'
    path.mkdir(parents=True)
    (home / 'config.yaml').write_text('kanban: {}\n')
    with kb.connect_closing() as conn:
        conn.execute(
            "INSERT INTO workspace_mount_roots(root, mount_path) VALUES (?, ?), (?, ?)",
            (str(broad), str(broad), str(specific), str(broad)),
        )

        from hermes_cli import kanban_workspace_policy as policy

        def validate(root, expected_mount=None):
            if root == specific:
                raise policy.WorkspaceUnavailable(f'workspaces_root_unmounted: {root}')
            return expected_mount or root

        monkeypatch.setattr(policy, 'validate_mount', validate)
        task_id = kb.create_task(
            conn, title='nested fence', assignee='default',
            workspace_kind='scratch', workspace_path=str(path),
        )
        result = kb.dispatch_once(conn, spawn_fn=lambda *_args, **_kw: None)
        assert task_id in result.stranded_by_mount_loss


def test_symlink_loop_refuses_one_task_without_aborting_board(home, monkeypatch):
    root = home / 'volume' / 'kanban-workspaces'
    root.mkdir(parents=True)
    configure(home, root)
    monkeypatch.setattr('os.path.ismount', lambda p: Path(p) == root.parent)
    kb.resolve_workspace(scratch(), board='default')
    loop = root / 'default' / 'loop'
    loop.symlink_to(loop)

    with kb.connect_closing() as conn:
        bad = kb.create_task(
            conn, title='loop', assignee='default',
            workspace_kind='scratch', workspace_path=str(loop),
        )
        durable = home / 'durable'
        good = kb.create_task(
            conn, title='good', assignee='default',
            workspace_kind='dir', workspace_path=str(durable),
        )
        calls = []
        result = kb.dispatch_once(
            conn, spawn_fn=lambda task, *_args, **_kw: calls.append(task.id),
        )
        assert bad in [item[0] for item in result.workspace_refused]
        assert good in calls


def test_unwritable_mount_refuses_before_claim(home, monkeypatch):
    root = home / 'volume' / 'kanban-workspaces'
    root.mkdir(parents=True)
    configure(home, root)
    monkeypatch.setattr('os.path.ismount', lambda p: Path(p) == root.parent)

    from hermes_cli import kanban_workspace_policy as policy

    real_open = policy.os.open

    def refuse_probe(path, flags, *args, **kwargs):
        if Path(path).name.startswith('.hermes-write-probe-') and flags & policy.os.O_CREAT:
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



# ---------------------------------------------------------------------------
# Recovery verb for stranded scratch cards (t_a0839b2e).
#
# 2026-09-24: a ramscratch force-recreate left 12 READY scratch cards refused
# every tick with stranded_by_mount_loss and NO verb to clear the dead
# persisted path. ``workspace reset`` is that verb; the dispatcher names it.
# ---------------------------------------------------------------------------

import shutil

from hermes_cli import kanban as kc
from hermes_cli.kanban_workspace_policy import STRANDED_RECOVERY_COMMAND


def _mounted_root(home, monkeypatch):
    root = home / 'volume' / 'kanban-workspaces'
    root.mkdir(parents=True)
    configure(home, root)
    monkeypatch.setattr('os.path.ismount', lambda p: Path(p) == root.parent)
    return root


def _stranded_card(conn, root, *, kind='scratch', title='stranded'):
    """A card whose persisted workspace lived on a volume that was recreated."""
    task_id = kb.create_task(conn, title=title, assignee='default')
    path = kb.resolve_workspace(
        SimpleNamespace(id=task_id, workspace_kind='scratch', workspace_path=None),
        board='default',
    )
    kb.set_workspace_path(conn, task_id, path)
    if kind != 'scratch':
        conn.execute('UPDATE tasks SET workspace_kind=? WHERE id=?', (kind, task_id))
    return task_id, path


def _lose_mount(root):
    # The live incident: the volume is force-recreated, so the root comes
    # back mounted + writable but every per-card directory under it is gone.
    shutil.rmtree(root)
    root.mkdir(parents=True)


def test_mount_loss_e2e_reset_lets_dispatcher_recreate(home, monkeypatch):
    root = _mounted_root(home, monkeypatch)
    with kb.connect_closing() as conn:
        task_id, path = _stranded_card(conn, root)
        conn.execute(
            "UPDATE task_workspace_survivors SET bases=? WHERE task_id=?",
            ('{"old-clone": "deadbeef"}', task_id),
        )
        _lose_mount(root)
        calls = []
        spawn = lambda task, *_a, **_k: calls.append(task.id)  # noqa: E731

        stuck = kb.dispatch_once(conn, spawn_fn=spawn)
        assert task_id in stuck.stranded_by_mount_loss
        assert not calls and not path.exists()

        out = kc.run_slash(f'workspace reset {task_id}')
        assert 'reset' in out.lower() and task_id in out, out
        task = kb.get_task(conn, task_id)
        assert task.workspace_path is None
        assert task.status == 'ready'
        # The stale baseline named repos in the DESTROYED tree; it must not
        # survive, or record_baseline (record-once) never re-baselines.
        assert conn.execute(
            'SELECT bases FROM task_workspace_survivors WHERE task_id=?', (task_id,),
        ).fetchone() is None
        kinds = [r[0] for r in conn.execute(
            'SELECT kind FROM task_events WHERE task_id=? ORDER BY id', (task_id,))]
        assert 'workspace_reset' in kinds

        healed = kb.dispatch_once(conn, spawn_fn=spawn)
        assert task_id not in healed.stranded_by_mount_loss
        assert task_id in calls
        assert path.is_dir()
        assert kb.get_task(conn, task_id).workspace_path == str(path)


@pytest.mark.parametrize('kind', ['worktree', 'dir'])
def test_reset_refuses_non_scratch(home, monkeypatch, kind):
    root = _mounted_root(home, monkeypatch)
    with kb.connect_closing() as conn:
        task_id, path = _stranded_card(conn, root, kind=kind)
        _lose_mount(root)
        ok, err = kb.reset_stranded_workspace(conn, task_id, actor='op')
        assert not ok and kind in err
        assert kb.get_task(conn, task_id).workspace_path == str(path)


def test_reset_refuses_when_path_still_exists(home, monkeypatch):
    root = _mounted_root(home, monkeypatch)
    with kb.connect_closing() as conn:
        task_id, path = _stranded_card(conn, root)
        ok, err = kb.reset_stranded_workspace(conn, task_id, actor='op')
        assert not ok and 'exists' in err
        assert kb.get_task(conn, task_id).workspace_path == str(path)


def test_reset_refuses_while_root_is_unmounted(home, monkeypatch):
    # Path missing because the MOUNT is gone: it may come back on remount, so
    # forgetting it would orphan real work. Refuse and name the real problem.
    root = _mounted_root(home, monkeypatch)
    with kb.connect_closing() as conn:
        task_id, path = _stranded_card(conn, root)
        shutil.rmtree(root)
        monkeypatch.setattr('os.path.ismount', lambda p: False)
        ok, err = kb.reset_stranded_workspace(conn, task_id, actor='op')
        assert not ok and 'workspaces_root_unmounted' in err
        assert kb.get_task(conn, task_id).workspace_path == str(path)


def test_reset_refuses_running_card(home, monkeypatch):
    root = _mounted_root(home, monkeypatch)
    with kb.connect_closing() as conn:
        task_id, path = _stranded_card(conn, root)
        _lose_mount(root)
        conn.execute("UPDATE tasks SET status='running' WHERE id=?", (task_id,))
        ok, err = kb.reset_stranded_workspace(conn, task_id, actor='op')
        assert not ok and 'running' in err
        assert kb.get_task(conn, task_id).workspace_path == str(path)


def test_reset_keeps_recorded_survivor_evidence(home, monkeypatch):
    root = _mounted_root(home, monkeypatch)
    with kb.connect_closing() as conn:
        task_id, _path = _stranded_card(conn, root)
        conn.execute(
            "UPDATE task_workspace_survivors SET bases=?, survivor=? WHERE task_id=?",
            ('{"repo": "abc"}', '{"kind": "pr", "ref": "o/r#1"}', task_id),
        )
        _lose_mount(root)
        ok, err = kb.reset_stranded_workspace(conn, task_id, actor='op')
        assert ok, err
        row = conn.execute(
            'SELECT bases, survivor FROM task_workspace_survivors WHERE task_id=?',
            (task_id,),
        ).fetchone()
        assert row['bases'] == '{}'
        assert row['survivor'] == '{"kind": "pr", "ref": "o/r#1"}'


def test_reset_all_stranded_touches_only_eligible_cards(home, monkeypatch):
    root = _mounted_root(home, monkeypatch)
    with kb.connect_closing() as conn:
        lost_a, _ = _stranded_card(conn, root, title='a')
        lost_b, _ = _stranded_card(conn, root, title='b')
        wt, wt_path = _stranded_card(conn, root, kind='worktree', title='wt')
        _lose_mount(root)
        alive = kb.create_task(conn, title='alive', assignee='default')
        alive_path = kb.resolve_workspace(
            SimpleNamespace(id=alive, workspace_kind='scratch', workspace_path=None),
            board='default',
        )
        kb.set_workspace_path(conn, alive, alive_path)

        dry = kc.run_slash('workspace reset --all-stranded --dry-run')
        assert lost_a in dry and lost_b in dry
        assert kb.get_task(conn, lost_a).workspace_path is not None

        out = kc.run_slash('workspace reset --all-stranded')
        assert lost_a in out and lost_b in out
        assert kb.get_task(conn, lost_a).workspace_path is None
        assert kb.get_task(conn, lost_b).workspace_path is None
        assert kb.get_task(conn, wt).workspace_path == str(wt_path)
        assert kb.get_task(conn, alive).workspace_path == str(alive_path)


def test_reset_cli_needs_exactly_one_target(home):
    out = kc.run_slash('workspace reset')
    assert '--all-stranded' in out


def test_stranded_refusal_names_the_recovery_command(home, monkeypatch, caplog):
    root = _mounted_root(home, monkeypatch)
    with kb.connect_closing() as conn:
        task_id, _ = _stranded_card(conn, root)
        _lose_mount(root)
        with caplog.at_level('WARNING'):
            kb.dispatch_once(conn, spawn_fn=lambda *_a, **_k: None)
        assert any(
            'stranded_by_mount_loss' in r.getMessage()
            and STRANDED_RECOVERY_COMMAND in r.getMessage()
            for r in caplog.records
        )
        payload = conn.execute(
            "SELECT payload FROM task_events WHERE task_id=? "
            "AND kind='stranded_by_mount_loss'", (task_id,),
        ).fetchone()[0]
        assert STRANDED_RECOVERY_COMMAND in payload


def test_restranding_after_reset_is_recorded_again(home, monkeypatch):
    # Reset recreates the SAME <root>/<board>/<id> path, so a second loss has a
    # byte-identical reason. The dedupe must not hide it behind the old event.
    root = _mounted_root(home, monkeypatch)
    with kb.connect_closing() as conn:
        task_id, path = _stranded_card(conn, root)
        _lose_mount(root)
        kb.dispatch_once(conn, spawn_fn=lambda *_a, **_k: None)
        assert kb.reset_stranded_workspace(conn, task_id, actor='op')[0]
        kb.set_workspace_path(conn, task_id, kb.resolve_workspace(
            kb.get_task(conn, task_id), board='default'))
        _lose_mount(root)
        kb.dispatch_once(conn, spawn_fn=lambda *_a, **_k: None)
        assert conn.execute(
            "SELECT count(*) FROM task_events WHERE task_id=? "
            "AND kind='stranded_by_mount_loss'", (task_id,),
        ).fetchone()[0] == 2


def _auto_unstrand(home, root, enabled):
    import yaml as _yaml
    cfg = _yaml.safe_load((home / 'config.yaml').read_text())
    cfg['kanban']['workspaces_auto_unstrand'] = enabled
    (home / 'config.yaml').write_text(_yaml.safe_dump(cfg))


def test_auto_unstrand_is_off_by_default(home, monkeypatch):
    root = _mounted_root(home, monkeypatch)
    with kb.connect_closing() as conn:
        task_id, _ = _stranded_card(conn, root)
        _lose_mount(root)
        for _ in range(2):
            result = kb.dispatch_once(conn, spawn_fn=lambda *_a, **_k: None)
            assert task_id in result.stranded_by_mount_loss
        assert kb.get_task(conn, task_id).workspace_path is not None


def test_auto_unstrand_heals_card_with_nothing_to_lose(home, monkeypatch):
    root = _mounted_root(home, monkeypatch)
    _auto_unstrand(home, root, True)
    with kb.connect_closing() as conn:
        task_id, path = _stranded_card(conn, root)
        _lose_mount(root)
        calls = []
        spawn = lambda task, *_a, **_k: calls.append(task.id)  # noqa: E731
        first = kb.dispatch_once(conn, spawn_fn=spawn)
        assert task_id not in first.stranded_by_mount_loss
        assert task_id not in [t for t, _ in first.workspace_refused]
        ev = conn.execute(
            "SELECT payload FROM task_events WHERE task_id=? AND kind='workspace_reset'",
            (task_id,),
        ).fetchone()
        assert ev is not None and 'auto' in ev[0]
        kb.dispatch_once(conn, spawn_fn=spawn)
        assert task_id in calls and path.is_dir()


def test_auto_unstrand_keeps_card_whose_worker_ran_without_remote_evidence(home, monkeypatch):
    root = _mounted_root(home, monkeypatch)
    _auto_unstrand(home, root, True)
    with kb.connect_closing() as conn:
        task_id, path = _stranded_card(conn, root)
        # A worker ran in the lost tree and no survivor pointer proves its work
        # reached a remote: auto-reset could discard unrecoverable work.
        kb._append_event(conn, task_id, 'spawned', {'pid': 4242})
        _lose_mount(root)
        result = kb.dispatch_once(conn, spawn_fn=lambda *_a, **_k: None)
        assert task_id in result.stranded_by_mount_loss
        assert kb.get_task(conn, task_id).workspace_path == str(path)


def test_auto_unstrand_heals_card_with_recorded_remote_survivor(home, monkeypatch):
    root = _mounted_root(home, monkeypatch)
    _auto_unstrand(home, root, True)
    with kb.connect_closing() as conn:
        task_id, _ = _stranded_card(conn, root)
        kb._append_event(conn, task_id, 'spawned', {'pid': 4242})
        conn.execute(
            "UPDATE task_workspace_survivors SET survivor=? WHERE task_id=?",
            ('{"kind": "pr", "ref": "o/r#1"}', task_id),
        )
        _lose_mount(root)
        calls = []
        result = kb.dispatch_once(
            conn, spawn_fn=lambda task, *_a, **_k: calls.append(task.id),
        )
        assert task_id not in result.stranded_by_mount_loss
        ev = conn.execute(
            "SELECT payload FROM task_events WHERE task_id=? AND kind='workspace_reset'",
            (task_id,),
        ).fetchone()
        assert ev is not None and 'survivor_recorded' in ev[0]
        # The startup scan heals it; the ready loop in the SAME tick recreates
        # the empty scratch dir and spawns.
        assert task_id in calls
        assert Path(kb.get_task(conn, task_id).workspace_path).is_dir()
