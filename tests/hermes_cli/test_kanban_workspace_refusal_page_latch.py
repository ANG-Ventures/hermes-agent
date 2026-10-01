"""The workspace-refusal #alerts page is post-on-change per card.

Live shape (t_ff4197d3 on board error-repair, 2026-09-27 09:11-09:39Z): one
card refused as ``workspaces_root_unmounted`` paged 5 times in 28 minutes.
The watcher latched in memory per board and re-armed on any tick whose
refused list was empty; the card is only admission-checked after the cap /
respawn-guard / provider gates, so skip ticks re-armed it, and the gateway
restart plus the Aegis failover dispatcher each paged again (t_ca81dfe2).
"""
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

import gateway.kanban_watchers as kw
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



def _tick(conn, notifier, pages):
    result = kbd.dispatch_once(conn, spawn_fn=lambda *_a, **_k: None)
    kw._observe_workspace_refusal_outages(notifier, [("default", result)])
    return result


@pytest.fixture
def pages(monkeypatch):
    sent = []
    monkeypatch.setattr(
        kw, "_send_workspace_refusal_alert",
        lambda board, summary: sent.append((board, summary)) or True,
    )
    return sent


def test_unmounted_current_root_pages_once_across_skips_restarts_and_failover(
    home, monkeypatch, pages,
):
    with kb.connect_closing() as conn:
        task_id, _old, root = _card_under_retired_root(home, monkeypatch, conn)
        _configure(home, root)  # still the CURRENT root: a real fault
        notifier = kw._WorkspaceRefusalOutageNotifier()
        result = _tick(conn, notifier, pages)
        assert task_id in [t for t, _ in result.workspace_refused]
        assert len(pages) == 1 and task_id in pages[0][1]
        assert "workspaces_root_unmounted" in pages[0][1]
        _tick(conn, notifier, pages)
        # Tick that skipped the card before admission (cap/guard/provider).
        kw._observe_workspace_refusal_outages(notifier, [("default", kb.DispatchResult())])
        _tick(conn, notifier, pages)
        # Gateway restart / Aegis failover dispatcher: fresh in-memory state.
        _tick(conn, kw._WorkspaceRefusalOutageNotifier(), pages)
        assert len(pages) == 1


def test_retired_root_card_is_reallocated_and_never_pages(home, monkeypatch, pages):
    with kb.connect_closing() as conn:
        task_id, _old, _root = _card_under_retired_root(home, monkeypatch, conn)
        notifier = kw._WorkspaceRefusalOutageNotifier()
        for _ in range(3):
            result = _tick(conn, notifier, pages)
            assert task_id not in [t for t, _ in result.workspace_refused]
        assert pages == []


def test_new_refusal_episode_or_new_reason_pages_again(home, monkeypatch, pages):
    with kb.connect_closing() as conn:
        task_id = kb.create_task(conn, title="t", assignee="default")
        notifier = kw._WorkspaceRefusalOutageNotifier()
        reason = "workspaces_root_unmounted: /Volumes/x"
        observe = lambda r: notifier.observe("default", [(task_id, r)], kw._send_workspace_refusal_alert)  # noqa: E731
        assert observe(reason) is True
        assert observe(reason) is False
        assert observe("workspaces_root_unwritable: /Volumes/x") is True
        # Admitted (claim ends the episode), later refused again: new page.
        kb._append_event(conn, task_id, "claimed", {})
        assert observe(reason) is True
        assert len(pages) == 3


def test_failed_delivery_releases_the_claim(home, monkeypatch):
    with kb.connect_closing() as conn:
        task_id = kb.create_task(conn, title="t", assignee="default")
        notifier = kw._WorkspaceRefusalOutageNotifier()
        refused = [(task_id, "workspaces_root_unmounted: /Volumes/x")]
        assert notifier.observe("default", refused, lambda *_a: False) is False
        assert not conn.execute(
            "SELECT 1 FROM task_events WHERE task_id=? AND kind='workspace_refusal_paged'",
            (task_id,),
        ).fetchone()
        assert notifier.observe("default", refused, lambda *_a: True) is True
        assert notifier.observe("default", refused, lambda *_a: True) is False
