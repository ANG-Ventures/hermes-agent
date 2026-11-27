"""Deterministic regressions from the operator's PR107 adjudication (no credentials)."""
import asyncio
import json
import sys
import threading
from types import SimpleNamespace

import pytest

from tests.tools._browser_use_daemon_fake import FakeDaemon, install
from tools import browser_supervisor as s, browser_use_cli as cli
from tools import browser_use_target as t, browser_vault_tool as v


@pytest.mark.parametrize('browser_use', [True, False])
def test_callback_failure_must_not_claim_nothing_written(monkeypatch, browser_use):
    route = t.BrowserUseRoute({}, 'ws://synthetic', False) if browser_use else None
    monkeypatch.setattr(cli, 'is_browser_use_cli_mode', lambda: browser_use)
    monkeypatch.setattr('tools.browser_tool._active_sessions', {})
    monkeypatch.setattr(t, 'route_for_task', lambda _: route)
    monkeypatch.setattr(t, 'begin', lambda *a, **kw: object())
    monkeypatch.setattr(t, 'end', lambda _: None)
    monkeypatch.setattr('tools.browser_tool_session.run_fenced', lambda session, fn: fn())
    writes = []
    def callback():
        writes.append('synthetic-write')
        raise RuntimeError('after write')
    result = json.loads(v._fenced_page_op('task', callback))
    assert writes
    assert 'Nothing was written' not in result['error']
    assert result['outcome'] == 'unknown'


@pytest.mark.parametrize('session', ['research', ''])
def test_daemon_selection_follows_backend_browser_across_tasks(monkeypatch, session):
    import subprocess
    monkeypatch.setattr(t, '_routes', {})
    monkeypatch.setattr(cli, '_find_cli', lambda: ['synthetic-cli'])
    monkeypatch.setattr(cli, '_base_subprocess_env', lambda: {})
    monkeypatch.setattr(cli, '_read_browser_cfg', lambda: {})
    monkeypatch.setattr(cli, '_workspace_dir', lambda _: None)
    monkeypatch.setattr(cli, '_attach_vault_supervisor', lambda *a: None)
    monkeypatch.setattr('tools.browser_tool_cdp._resolve_cdp_override', lambda url: url)
    browsers, daemons = {}, {}
    def route(env, name, task, local):
        key = cli._backend_cache_key(task, name)
        env['BU_CDP_WS'] = browsers.setdefault(key, 'ws://synthetic/' + key)
        env[cli._PRIVATE_BROWSER_SENTINEL] = '1'
    monkeypatch.setattr(cli, '_route_backend', route)
    def execute(cmd, code, env, timeout):
        # Harness startup selects T1; later calls retain that daemon's selection.
        selected = daemons.setdefault(env['BU_NAME'], 'T1')
        if code == 'select T2':
            daemons[env['BU_NAME']] = 'T2'
            selected = 'T2'
        return subprocess.CompletedProcess(cmd, 0, selected, '')
    monkeypatch.setattr(cli, '_run_cli_killing_process_group', execute)
    cli.browser_exec('select T2', session=session, task_id='taskA')
    route_a = t.route_for_task('taskA')
    cli.browser_exec('inspect', session=session, task_id='taskB')
    route_b = t.route_for_task('taskB')
    assert (route_a.cdp_url == route_b.cdp_url) == bool(session)
    assert daemons[route_b.env['BU_NAME']] == ('T2' if session else 'T1')
    assert (route_a.env['BU_NAME'] == route_b.env['BU_NAME']) == bool(session)
    install(monkeypatch, t, FakeDaemon(observe=lambda r: (123, daemons[r.env['BU_NAME']])))
    if session:
        with pytest.raises(RuntimeError, match='route changed'):
            t.begin('taskA', expected_route=route_a)
        cli.browser_exec('inspect', session=session, task_id='taskA')
        route_a = t.route_for_task('taskA')
    token = t.begin('taskA', expected_route=route_a)
    try:
        with pytest.raises(RuntimeError, match='not pinned'):
            t.supervisor_for_task('taskB')
        with t._pin.get().dispatch():
            pass
    finally:
        t.end(token)
    with pytest.raises(RuntimeError, match='route changed'):
        t.begin('taskB', expected_route=route_a)


def test_selection_change_waits_for_dispatch_ack(monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    monkeypatch.setattr(t, '_routes', {})
    monkeypatch.setattr('tools.browser_tool_cdp._resolve_cdp_override', lambda url: url)
    env = {'BU_NAME': 'shared', 'BU_CDP_WS': 'ws://synthetic'}
    route = t.record_route('taskA', env)
    install(monkeypatch, t, FakeDaemon(observe=lambda _: (1, 'login')))
    token = t.begin('taskA', expected_route=route)
    tx = t._pin.get()
    attempted, changed = threading.Event(), threading.Event()
    def select():
        attempted.set()
        with t.selection_execution('taskB', env) as complete:
            changed.set()
            complete(True)
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            with tx.dispatch():
                future = pool.submit(select)
                assert attempted.wait(5)
                assert not future.done()
                assert not changed.is_set()
                assert not t._lock.acquire(blocking=False)
            future.result(5)
            assert changed.is_set()
        with pytest.raises(RuntimeError, match='route changed'):
            with tx.dispatch():
                pytest.fail('stale transaction authorized')
    finally:
        t.end(token)


def test_probe_cannot_import_cwd_or_inherited_pythonpath(monkeypatch, tmp_path):
    marker = tmp_path / 'executed'
    (tmp_path / 'browser_harness.py').write_text(
        f'from pathlib import Path\nPath({str(marker)!r}).touch()\n')
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv('PYTHONPATH', str(tmp_path))
    monkeypatch.setattr(cli, '_find_cli', lambda: [sys.executable, '-m', 'browser_harness.run'])
    route = t.BrowserUseRoute({'PYTHONPATH': str(tmp_path)}, 'ws://synthetic', False)
    with pytest.raises(RuntimeError):
        t._daemon_request(route, {'meta': 'current_tab'})
    assert not marker.exists()


def test_route_replaced_in_final_probe_cannot_dispatch(monkeypatch):
    monkeypatch.setattr(t, '_routes', {})
    monkeypatch.setattr('tools.browser_tool_cdp._resolve_cdp_override', lambda url: url)
    route = t.record_route('task', {'BU_CDP_WS': 'ws://synthetic'})
    daemon = install(monkeypatch, t, FakeDaemon(observe=lambda _: (1, 'login')))
    calls = []
    monkeypatch.setattr(s.SUPERVISOR_REGISTRY, 'get_or_start', lambda **kw: SimpleNamespace(
        evaluate_runtime=lambda *a, **kw: calls.append(a) or {'ok': True}))
    token = t.begin('task', expected_route=route)
    try:
        view = t.supervisor_for_task('task')
        def swap(_):
            t.record_route('task', {'BU_CDP_WS': 'ws://replacement'})
            return (1, 'login')
        daemon.observe = swap
        assert not view.evaluate_runtime('synthetic-write')['ok']
        assert not calls
    finally:
        t.end(token)


@pytest.mark.parametrize('change', ['route', 'lease', 'deadline', 'shared_selection'])
def test_supervisor_dispatch_rechecks_transaction_after_setup(monkeypatch, change):
    import time
    from dataclasses import replace
    from tools.bot_desktop import lease
    monkeypatch.setattr(t, '_routes', {})
    monkeypatch.setattr('tools.browser_tool_cdp._resolve_cdp_override', lambda url: url)
    epoch = [7]
    monkeypatch.setattr(lease, 'assert_agent_may_act', lambda profile=None: SimpleNamespace(epoch=epoch[0]))
    env = {'BU_CDP_WS': 'ws://synthetic', 'BU_NAME': 'shared-research'}
    route = t.record_route('task', env, local=True)
    selected = ['login']
    install(monkeypatch, t, FakeDaemon(observe=lambda _: (1, selected[0])))
    if change == 'shared_selection':
        import subprocess
        monkeypatch.setattr(cli, '_find_cli', lambda: ['synthetic-cli'])
        monkeypatch.setattr(cli, '_base_subprocess_env', lambda: {})
        monkeypatch.setattr(cli, '_read_browser_cfg', lambda: {})
        monkeypatch.setattr(cli, '_workspace_dir', lambda _: None)
        monkeypatch.setattr(cli, '_attach_vault_supervisor', lambda *a: None)
        def route_backend(child_env, *args):
            child_env['BU_CDP_WS'] = env['BU_CDP_WS']
            child_env[cli._PRIVATE_BROWSER_SENTINEL] = '1'
        monkeypatch.setattr(cli, '_route_backend', route_backend)
        def execute(cmd, code, child_env, timeout):
            selected[0] = 'other-tab'
            return subprocess.CompletedProcess(cmd, 0, '', '')
        monkeypatch.setattr(cli, '_run_cli_killing_process_group', execute)
        cli.browser_exec('inspect', session='research', task_id='taskB')
        env = dict(t.route_for_task('taskB').env)
        route = t.record_route('task', env, local=True)
        selected[0] = 'login'
    token = t.begin('task', expected_route=route)
    tx = t._pin.get()
    if change == 'deadline':
        tx = replace(tx, deadline=time.monotonic() - 1)
    sup = s.CDPSupervisor('synthetic', 'ws://synthetic')
    sup._loop = SimpleNamespace(is_running=lambda: True)
    sup._active, sup._page_session_id = True, 'initial'
    calls = []
    async def cdp(method, params=None, **kw):
        calls.append(method)
        if method == 'Target.getTargetInfo':
            return {'result': {'targetInfo': {'type': 'page', 'targetId': 'login'}}}
        if method == 'Target.attachToTarget':
            if change == 'route':
                t.record_route('task', {'BU_CDP_WS': 'ws://replacement'})
            elif change == 'lease':
                epoch[0] += 1
            elif change == 'shared_selection':
                cli.browser_exec('select other-tab', session='research', task_id='taskB')
                assert selected[0] == 'other-tab'
            return {'result': {'sessionId': 'pinned'}}
        return {'result': {}}
    sup._cdp = cdp
    monkeypatch.setattr(s, '_schedule', lambda coro, loop, **kw: asyncio.run(coro))
    try:
        result = sup.evaluate_runtime('synthetic-write', target_id='login', dispatch_guard=tx.dispatch)
        assert not result['ok']
        assert result['outcome'] == 'not_dispatched'
        assert 'Runtime.evaluate' not in calls
    finally:
        t.end(token)


@pytest.mark.platforms('posix')
@pytest.mark.parametrize('layout', ['interpreter', 'pm-generation'])
def test_isolated_probe_real_installed_harness_ipc(monkeypatch, tmp_path, layout):
    import socket
    import zipfile
    import tempfile
    from pathlib import Path
    wheel = Path(__file__).resolve().parents[2] / 'deps/browser_harness-0.1.13-py3-none-any.whl'
    from pm import environments
    monkeypatch.setattr(environments, 'dependency_home_root', lambda: tmp_path / 'home')
    project = Path(t.__file__).resolve().parents[1]
    site = tmp_path / 'site'
    if layout == 'pm-generation':
        generation = environments.install_state_dir(project) / 'environments' / 'synthetic' / 'venv'
        generation.mkdir(parents=True)
        (generation / 'pyvenv.cfg').write_text(
            f'version = {sys.version_info.major}.{sys.version_info.minor}.0\n')
        site = environments.site_packages(generation)
        environments.runtime_facts_path(project).write_text(json.dumps(
            {'packages': {'venv': {'environment': str(generation)}}}))
    if wheel.exists():
        with zipfile.ZipFile(wheel) as archive:
            archive.extractall(site)
    else:
        if layout == 'pm-generation':
            pytest.skip('PM generation fixture requires optional browser-harness wheel')
        from importlib.metadata import distribution, PackageNotFoundError
        try:
            site = Path(distribution('browser-harness').locate_file(''))
        except PackageNotFoundError:
            pytest.skip('browser-harness not installed; optional downloaded wheel unavailable')
    private_runtime = tempfile.TemporaryDirectory(prefix='bu-')
    runtime = Path(private_runtime.name)
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(runtime / 'bu.sock'))
    server.listen()
    server.settimeout(5)
    requests = []
    def serve():
        for _ in range(2):
            conn, _ = server.accept()
            with conn:
                request = json.loads(conn.recv(4096))
                requests.append(request['meta'])
                reply = {'pid': 123} if request['meta'] == 'ping' else {'targetId': 'login'}
                conn.sendall(json.dumps(reply).encode() + b'\n')
    thread = threading.Thread(target=serve)
    thread.start()
    if layout == 'pm-generation':
        assert environments.committed_venv(project) == generation
        marker = tmp_path / 'executed'
        spoof = tmp_path / 'untrusted'
        spoof.mkdir()
        (spoof / 'browser_harness.py').write_text(
            f'from pathlib import Path\nPath({str(marker)!r}).touch()\n')
        monkeypatch.chdir(spoof)
        monkeypatch.setenv('PYTHONPATH', str(spoof))
        monkeypatch.setattr('sysconfig.get_path', lambda _: str(tmp_path / 'empty-store-site'))
    else:
        monkeypatch.setattr('sysconfig.get_path', lambda _: str(site))
    route = t.BrowserUseRoute({'BU_NAME': 'scoped', 'BH_RUNTIME_DIR': str(runtime),
        'BH_TMP_DIR': str(runtime), 'PYTHONPATH': str(tmp_path / 'untrusted')}, 'ws://synthetic', False)
    try:
        assert t._daemon_request(route, {'meta': 'current_tab'}) == (123, {'targetId': 'login'})
        if layout == 'pm-generation':
            assert not marker.exists()
        thread.join(5)
        assert requests == ['ping', 'current_tab']
    finally:
        server.close()
        thread.join(5)

@pytest.mark.parametrize('phase', ['setup', 'dispatch'])
def test_real_loop_timeout_revokes_setup_or_reports_unknown(phase):
    loop = asyncio.new_event_loop()
    started, stopped = threading.Event(), threading.Event()
    writes = []
    def worker():
        asyncio.set_event_loop(loop)
        started.set()
        loop.run_forever()
    thread = threading.Thread(target=worker)
    thread.start()
    assert started.wait(5)
    sup = s.CDPSupervisor('synthetic', 'ws://synthetic')
    sup._loop, sup._active, sup._page_session_id = loop, True, 'initial'
    async def cdp(method, params=None, **kw):
        if method == 'Target.getTargetInfo':
            if phase == 'setup':
                try:
                    await asyncio.Event().wait()
                finally:
                    stopped.set()
            return {'result': {'targetInfo': {'type': 'page', 'targetId': 'login'}}}
        if method == 'Target.attachToTarget':
            return {'result': {'sessionId': 'pinned'}}
        if method == 'Runtime.evaluate':
            writes.append('synthetic-write')
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()
        return {'result': {}}
    sup._cdp = cdp
    try:
        result = sup.evaluate_runtime('synthetic-write', target_id='login', timeout=.05)
        assert not result['ok']
        assert stopped.wait(2), 'timeout returned without quiescing the coroutine'
        assert result['outcome'] == ('not_dispatched' if phase == 'setup' else 'unknown')
        assert bool(writes) == (phase == 'dispatch')
        # Round-trip the loop after return: revoked setup must not dispatch later.
        asyncio.run_coroutine_threadsafe(asyncio.sleep(0), loop).result(2)
        assert bool(writes) == (phase == 'dispatch')
    finally:
        async def drain():
            tasks = [task for task in asyncio.all_tasks() if task is not asyncio.current_task()]
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        asyncio.run_coroutine_threadsafe(drain(), loop).result(2)
        loop.call_soon_threadsafe(loop.stop)
        thread.join(5)
        loop.close()


def test_other_process_cannot_move_shared_selection_before_dispatch(monkeypatch):
    """P1 d118eabaa559: a second process sharing the named daemon switches its tab while this
    vault op awaits Target.attachToTarget. The daemon must own the selection, so the write only
    ever reaches the tab the daemon still has selected."""
    monkeypatch.setattr(t, '_routes', {})
    monkeypatch.setattr('tools.browser_tool_cdp._resolve_cdp_override', lambda url: url)
    daemon = install(monkeypatch, t, FakeDaemon(target='login'))
    route = t.record_route('task', {'BU_CDP_WS': 'ws://synthetic', 'BU_NAME': 'shared-research'})
    token = t.begin('task', expected_route=route)
    tx = t._pin.get()
    sup = s.CDPSupervisor('synthetic', 'ws://synthetic')
    sup._loop = SimpleNamespace(is_running=lambda: True)
    sup._active, sup._page_session_id = True, 'initial'
    switches, dispatched = [], []
    async def cdp(method, params=None, **kw):
        if method == 'Target.getTargetInfo':
            return {'result': {'targetInfo': {'type': 'page', 'targetId': params['targetId']}}}
        if method == 'Target.attachToTarget':
            # Process B, straight over the daemon's IPC (no Hermes lock involved).
            switches.append(daemon.request(route, {'meta': 'set_session', 'session_id': 's-T2',
                                                   'target_id': 'other-tab'})[1])
            return {'result': {'sessionId': 'pinned'}}
        if method == 'Runtime.evaluate':
            dispatched.append(daemon.target)
        return {'result': {}}
    sup._cdp = cdp
    monkeypatch.setattr(s, '_schedule', lambda coro, loop, **kw: asyncio.run(coro))
    try:
        tx.validate()
        sup.evaluate_runtime('synthetic-write', target_id=tx.target, dispatch_guard=tx.dispatch)
    finally:
        t.end(token)
    assert switches and 'error' in switches[0], 'shared daemon accepted a selection move during the write'
    assert dispatched in ([], ['login']), f'write dispatched while the daemon had {dispatched} selected'
    assert daemon.lease is None, 'lease must be released when the vault op ends'
    assert daemon.switch('other-tab') == {'session_id': 's-other-tab'}


def test_native_selection_retires_stale_browser_use_route(monkeypatch):
    """P1 ccb1c23940f1: once the native browser stack is current, a Browser Use route recorded
    earlier for the same task must not route vault ops to the old Browser Use tab."""
    monkeypatch.setattr(t, '_routes', {})
    monkeypatch.setattr('tools.browser_tool_cdp._resolve_cdp_override', lambda url: url)
    t.record_route('task', {'BU_CDP_WS': 'ws://old-browser-use', 'BU_NAME': 'shared'})
    monkeypatch.setattr(cli, 'is_browser_use_cli_mode', lambda: False)
    native = SimpleNamespace(name='native-supervisor')
    monkeypatch.setattr(s.SUPERVISOR_REGISTRY, 'get', lambda task_id: native if task_id == 'task' else None)
    monkeypatch.setattr('tools.browser_tool._active_sessions', {'task': {'features': {'local': False}}})
    monkeypatch.setattr(t, 'begin', lambda *a, **kw: pytest.fail('vault op pinned the stale Browser Use route'))
    fenced = []
    monkeypatch.setattr('tools.browser_tool_session.run_fenced', lambda session, fn: fenced.append(session) or fn())
    assert v._ensure_supervisor('task') is native
    assert json.loads(v._fenced_page_op('task', lambda: json.dumps({'success': True}))) == {'success': True}
    assert fenced == [{'features': {'local': False}}]
    assert t.route_for_task('task') is None


def test_queued_exec_rechecks_desktop_lease_after_selection_wait(monkeypatch):
    """P1 6b3e4e4d1085: a local exec admitted by run_fenced then queued behind another exec's
    selection lock must not launch if a human took the screen while it waited."""
    import subprocess
    from tools.bot_desktop import lease
    monkeypatch.setattr(t, '_routes', {})
    monkeypatch.setattr('tools.browser_tool_cdp._resolve_cdp_override', lambda url: url)
    monkeypatch.setattr(cli, '_find_cli', lambda: ['synthetic-cli'])
    monkeypatch.setattr(cli, '_base_subprocess_env', lambda: {})
    monkeypatch.setattr(cli, '_read_browser_cfg', lambda: {})
    monkeypatch.setattr(cli, '_workspace_dir', lambda _: None)
    monkeypatch.setattr(cli, '_attach_vault_supervisor', lambda *a: None)
    def route_backend(env, *args):
        env['BU_CDP_WS'] = 'ws://bot-desktop'
        env[cli._BOT_DESKTOP_BROWSER_SENTINEL] = '1'
        env[cli._PRIVATE_BROWSER_SENTINEL] = '1'
    monkeypatch.setattr(cli, '_route_backend', route_backend)
    state = {'epoch': 4, 'human': False}
    def may_act(profile=None):
        if state['human']:
            raise lease.HumanHasControl('human has control')
        return SimpleNamespace(epoch=state['epoch'])
    monkeypatch.setattr(lease, 'assert_agent_may_act', may_act)
    monkeypatch.setattr(lease, 'get', lambda profile=None: SimpleNamespace(epoch=state['epoch']))
    monkeypatch.setattr('tools.browser_tool_session._shares_bot_desktop_browser', lambda info: True)
    real_lock = t._lock
    class TakeoverWhileQueued:
        # Execution A held the selection lock; the human took over before B acquired it.
        def __enter__(self):
            state['human'], state['epoch'] = True, state['epoch'] + 1
            return real_lock.__enter__()
        def __exit__(self, *exc):
            return real_lock.__exit__(*exc)
    monkeypatch.setattr(t, '_lock', TakeoverWhileQueued())
    launched = []
    monkeypatch.setattr(cli, '_run_cli_killing_process_group',
                        lambda cmd, code, env, timeout: launched.append(code) or subprocess.CompletedProcess(cmd, 0, '', ''))
    result = json.loads(cli.browser_exec('click("Submit")', task_id='taskB'))
    assert launched == [], 'queued exec ran against the human-controlled browser'
    assert result.get('code') == 'human_has_control'
