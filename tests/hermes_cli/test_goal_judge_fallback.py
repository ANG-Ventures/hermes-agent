"""Bounded verdict chains exercise real auxiliary HTTP calls in disposable homes."""
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

import pytest

from hermes_cli import goals


@pytest.fixture
def endpoint():
    calls = []
    replies = {}
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            model = body['model']
            calls.append(model)
            if '_headers' in replies:
                replies['_headers'].append(self.headers.get('x-session-id'))
            value = replies.get(model, 503)
            if callable(value):
                value = value()
            if value is None:  # a hung route released after its client was killed: no reply
                return
            if isinstance(value, int):
                self.send_response(value)
                self.end_headers()
                self.wfile.write(b'{"error":{"message":"unavailable"}}')
                return
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({'id': 'test', 'model': model, 'choices': [
                {'index': 0, 'finish_reason': 'stop', 'message': {'role': 'assistant', 'content': value}}
            ], 'usage': {'prompt_tokens': 11, 'completion_tokens': 3, 'total_tokens': 14}}).encode())
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}/v1', calls, replies
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def configure(home, url, **extra):
    home.mkdir(exist_ok=True)
    route = dict(provider='custom', base_url=url, api_key='test-only', timeout=15)
    config = dict(route, model='primary', total_timeout=60,
                  fallback_chain=[dict(route, model=m) for m in ('sol', 'grok', 'kimi')], **extra)
    (home / 'config.yaml').write_text(json.dumps({'auxiliary': {'goal_judge': config}}), encoding='utf-8')


@pytest.mark.slow_test(120, reason='Real fresh-interpreter provider resolution on up to four sequential routes')
@pytest.mark.parametrize('winner,verdict', [
    ('primary', 'done'), ('sol', 'done'), ('grok', 'done'), ('kimi', 'done'),
    (None, 'done'), ('sol', 'blocked'), ('sol', 'wait'), ('sol', 'continue'),
])
def test_ordered_chain_and_failure_accounting(tmp_path, monkeypatch, endpoint, winner, verdict):
    url, calls, replies = endpoint
    configure(tmp_path, url)
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    if winner:
        replies[winner] = json.dumps(dict(verdict=verdict, reason='evidence', wait_for_seconds=1))
    manager = goals.GoalManager('test-chain')
    manager.set('produce verified artifact')
    result = manager.evaluate_after_turn('substantive evidence')
    order = ['primary', 'sol', 'grok', 'kimi']
    assert calls == (order[:order.index(winner) + 1] if winner else order)
    assert manager.state.consecutive_transport_failures == (0 if winner else 1)
    assert result['verdict'] == (verdict if winner else 'continue')
    assert (manager.state.status == 'done') == (winner is not None and verdict == 'done')


@pytest.mark.parametrize('raw', ['', '{}', '{"done":"false"}', '{"done":{}}', '{"verdict":"bogus"}', 'not json'])
def test_malformed_cannot_complete_and_success_resets(tmp_path, monkeypatch, endpoint, raw):
    url, calls, replies = endpoint
    configure(tmp_path, url)
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    replies['primary'] = raw
    replies['sol'] = '{"verdict":"continue","reason":"more work"}'
    manager = goals.GoalManager('test-malformed')
    manager.set('verify work')
    manager.state.consecutive_transport_failures = 2
    manager.state.consecutive_parse_failures = 2
    manager._save()
    result = manager.evaluate_after_turn('work remains')
    assert calls == ['primary', 'sol']
    assert result['verdict'] == 'continue'
    assert manager.state.consecutive_transport_failures == 0
    assert manager.state.consecutive_parse_failures == 0


def test_profile_chain_isolation(tmp_path, monkeypatch, endpoint):
    from hermes_constants import set_hermes_home_override
    url, calls, replies = endpoint
    replies['primary'] = '{"verdict":"continue","reason":"確認 — café 🚀"}'
    for name in ('a', 'b', 'a'):
        home = tmp_path / name
        configure(home, url)
        set_hermes_home_override(home)
        try:
            result = goals.judge_goal('verify', 'evidence')
            assert result[0] == 'continue' and not result[2] and not result[4]
            assert result[1] == '確認 — café 🚀'
            from agent.auxiliary_client import _client_cache_key
            assert str(home) in str(_client_cache_key('custom', async_mode=False, model='primary'))
        finally:
            set_hermes_home_override(None)
    assert calls == ['primary', 'primary', 'primary']


def test_single_attempt_custom_does_not_discover_cloud(monkeypatch):
    from agent import auxiliary_client as ac
    from agent.auxiliary_attempt import single_attempt
    cloud = []
    monkeypatch.setattr(ac, '_try_custom_endpoint', lambda: (None, None))
    monkeypatch.setattr(ac, '_resolve_api_key_provider', lambda: cloud.append(True) or (None, None))
    for provider in ('custom', 'ollama'):
        with single_attempt('goal_judge', dict(provider=provider, model='local-model')):
            client, _ = ac.resolve_provider_client(provider, 'local-model', task='goal_judge')
            assert client is None
    assert cloud == []


def test_child_usage_reaches_parent_session_and_ledger(tmp_path, monkeypatch, endpoint):
    from agent import aux_accounting as accounting
    import agent.chat_completion_helpers as helpers
    from unittest.mock import Mock
    url, calls, replies = endpoint
    configure(tmp_path, url)
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    replies['primary'] = '{}'
    replies['sol'] = '{"verdict":"continue","reason":"valid"}'
    db = Mock()
    ledger = Mock()
    monkeypatch.setattr(helpers, '_emit_aux_api_call_record', ledger)
    session_token = accounting.set_accounting_context(db, 'owned-session')
    ledger_token = accounting.set_blackbox_turn(object(), 'owned-turn')
    try:
        result = goals.judge_goal('verify', 'evidence')
    finally:
        accounting.reset_blackbox_turn(ledger_token)
        accounting.reset_accounting_context(session_token)
    assert result[0] == 'continue' and not result[2] and not result[4]
    assert calls == ['primary', 'sol']
    assert db.record_auxiliary_usage.call_count == ledger.call_count == 2
    assert [call.kwargs['model'] for call in db.record_auxiliary_usage.call_args_list] == calls
    for call in db.record_auxiliary_usage.call_args_list:
        assert call.args == ('owned-session', 'goal_judge')
        assert call.kwargs['input_tokens'] == 11
        assert call.kwargs['output_tokens'] == 3
    assert [call.kwargs['model'] for call in ledger.call_args_list] == calls
    assert all(call.kwargs['usage'].input_tokens == 11 for call in ledger.call_args_list)


def test_deadline_and_cancel_reap_request_owner(tmp_path, monkeypatch):
    import subprocess
    import sys
    from hermes_cli import goal_judge_chain as chain
    from agent.auxiliary_client import AuxiliaryExplicitCancellation, aux_interrupt_protection
    actual_popen = subprocess.Popen
    children = []
    def spawn(*args, **kwargs):
        # An uncooperative provider/socket owner: deadline must terminate, not abandon it.
        import os
        assert kwargs['start_new_session'] == (os.name == 'posix')
        proc = actual_popen([sys.executable, '-c', 'import time; time.sleep(60)'], **kwargs)
        children.append(proc)
        return proc
    monkeypatch.setattr(chain.subprocess, 'Popen', spawn)
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    route = dict(provider='custom', model='test', timeout=1)
    assert chain.run_attempt(route, [], 10, time.monotonic() + 0.2)['category'] in {'timeout', 'cleanup_pending'}
    assert chain._cleanup_finished(str(tmp_path), timeout=2)
    assert children[-1].poll() is not None
    cancel = threading.Event()
    timer = threading.Timer(0.2, cancel.set)
    timer.start()
    try:
        with aux_interrupt_protection(active=False, cancel_event=cancel):
            with pytest.raises(AuxiliaryExplicitCancellation):
                chain.run_attempt(route, [], 10, time.monotonic() + 30)
    finally:
        timer.cancel()
        timer.join()
    assert chain._cleanup_finished(str(tmp_path), timeout=2)
    assert children[-1].poll() is not None


def test_total_budget_and_invalid_config(tmp_path, monkeypatch):
    from hermes_cli import goal_judge_chain as chain
    import hermes_cli.config as config
    monkeypatch.setattr(config, 'load_config', lambda: {'auxiliary': {'goal_judge': {
        'provider': 'custom', 'model': 'primary', 'total_timeout': float('inf'),
        'fallback_chain': [{'provider': 'custom', 'model': 'second'}],
    }}})
    with pytest.raises(ValueError):
        chain.chain_config(1)
    calls = []
    def attempt(route, messages, max_tokens, deadline, **kwargs):
        calls.append(route['model'])
        assert deadline <= started + 0.21
        time.sleep(max(0, deadline - time.monotonic()))
        return dict(category='timeout')
    monkeypatch.setattr(chain, 'run_attempt', attempt)
    routes = [dict(provider='custom', model=m, timeout=10) for m in ('primary', 'second')]
    started = time.monotonic()
    result = chain.judge_chain((routes, 0.2), 'system', 'prompt', 10, goals._parse_judge_response)
    assert calls == ['primary']
    assert result[0] == 'continue' and result[4]


def test_pause_cancels_chain_without_failure_increment(tmp_path, monkeypatch):
    from hermes_cli import goal_judge_chain as chain
    from agent.auxiliary_client import AuxiliaryExplicitCancellation
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    configure(tmp_path, 'http://127.0.0.1:9/v1')
    manager = goals.GoalManager('cancel-chain')
    manager.set('deliver')
    def attempt(route, messages, max_tokens, deadline, cancel_check=None, **kwargs):
        goals.GoalManager('cancel-chain').pause('user stop')
        assert cancel_check()
        raise AuxiliaryExplicitCancellation()
    monkeypatch.setattr(chain, 'run_attempt', attempt)
    with pytest.raises(AuxiliaryExplicitCancellation):
        manager.evaluate_after_turn('working')
    persisted = goals.load_goal('cancel-chain')
    assert persisted.status == 'paused'
    assert persisted.consecutive_transport_failures == 0
    assert persisted.turns_used == 0


@pytest.mark.parametrize('change', ['pause', 'replace', 'resume', 'clear'])
@pytest.mark.parametrize('verdict', ['done', 'continue'])
def test_no_db_goal_change_cancels_inflight_verdict(tmp_path, monkeypatch, endpoint, change, verdict):
    from agent.auxiliary_client import AuxiliaryExplicitCancellation
    url, calls, replies = endpoint
    configure(tmp_path, url)
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setattr(goals, '_get_session_db', lambda: None)
    manager = goals.GoalManager('memory-stop')
    manager.set('deliver')
    changed = []

    def respond():
        # The real child is waiting on HTTP when the owning manager changes.
        if change == 'replace':
            manager.set('replacement')
        elif change == 'clear':
            manager.clear()
        else:
            manager.pause('user stop')
            if change == 'resume':
                manager.resume()
        changed.append(manager.state.to_json() if manager.state else None)
        return json.dumps(dict(verdict=verdict, reason='late verdict'))

    replies['primary'] = respond
    with pytest.raises(AuxiliaryExplicitCancellation):
        manager.evaluate_after_turn('evidence')
    assert calls == ['primary']
    assert changed == [manager.state.to_json() if manager.state else None]
    if manager.state:
        assert manager.state.consecutive_transport_failures == 0
        assert manager.state.consecutive_parse_failures == 0


@pytest.mark.parametrize('failure', ['spawn', 'json', 'shape', 'exit', 'pipe'])
def test_worker_failure_advances_without_false_completion(tmp_path, monkeypatch, failure):
    import subprocess
    from hermes_cli import goal_judge_chain as chain
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    class Worker:
        pid = 123
        returncode = 1 if failure == 'exit' else 0
        def communicate(self, *args, **kwargs):
            if failure == 'pipe':
                raise BrokenPipeError()
            return ('[]' if failure == 'shape' else 'not json'), None
        def poll(self):
            return self.returncode
        def wait(self, timeout=None):
            assert timeout is not None
            return self.returncode
    def spawn(*args, **kwargs):
        if failure == 'spawn':
            raise OSError('private error must not escape')
        return Worker()
    monkeypatch.setattr(chain.subprocess, 'Popen', spawn)
    original = chain.run_attempt
    calls = []
    def attempt(route, *args, **kwargs):
        calls.append(route['model'])
        if route['model'] == 'primary':
            return original(route, *args, **kwargs)
        return dict(category='ok', content='{"verdict":"blocked","reason":"missing input"}')
    monkeypatch.setattr(chain, 'run_attempt', attempt)
    routes = [dict(provider='custom', model=m, timeout=2) for m in ('primary', 'second')]
    result = chain.judge_chain((routes, 4), 'system', 'prompt', 10, goals._parse_judge_response)
    assert calls == ['primary', 'second']
    assert result[0] == 'blocked' and not result[2] and not result[4]


def test_already_reaped_worker_does_not_wait_for_cleanup_thread(tmp_path, monkeypatch):
    from hermes_cli import goal_judge_chain as chain
    from types import SimpleNamespace
    finished = threading.Event()
    home = str(tmp_path)
    chain._CLEANUPS[home] = {finished}
    monkeypatch.setattr(chain.threading, 'Thread', lambda **kw: pytest.fail('already reaped'))
    assert chain._stop(SimpleNamespace(returncode=0), home, time.monotonic(), finished)
    assert finished.is_set() and home not in chain._CLEANUPS


@pytest.mark.parametrize('cancel', [False, True])
def test_stuck_cleanup_retains_owner_and_inhibits_retry(tmp_path, monkeypatch, cancel):
    from hermes_cli import goal_judge_chain as chain
    from agent.auxiliary_client import AuxiliaryExplicitCancellation
    import agent.deadline
    release = threading.Event()
    cleanup_started = threading.Event()
    spawned = []
    class Worker:
        pid = 123
        returncode = None
        def communicate(self, *args, **kwargs):
            raise __import__('subprocess').TimeoutExpired('worker', 0)
        def poll(self):
            return None if not release.is_set() else -9
        def wait(self, timeout=None):
            assert timeout is not None
            return -9
    def kill(pid):
        cleanup_started.set()
        release.wait(5)
        raise OSError('kill failed')
    monkeypatch.setattr(agent.deadline, 'kill_process_tree', kill)
    if chain.os.name == 'posix':
        monkeypatch.setattr(chain.os, 'killpg', lambda pid, sig: kill(pid))
    monkeypatch.setattr(chain.subprocess, 'Popen', lambda *a, **kw: spawned.append(Worker()) or spawned[-1])
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    route = dict(provider='custom', model='primary', timeout=0.3)
    started = time.monotonic()
    try:
        if cancel:
            with pytest.raises(AuxiliaryExplicitCancellation):
                chain.run_attempt(route, [], 10, started + 0.3, cancel_check=lambda: True)
        else:
            result = chain.judge_chain(([route, route], 0.3), 's', 'p', 10, goals._parse_judge_response)
            # Its own stuck reap does not stop the chain; the spent budget does. Never done.
            assert result[0] == 'continue' and result[4] and 'total_timeout' in result[1]
        assert time.monotonic() - started < 2
        assert cleanup_started.wait(1)
        assert chain.run_attempt(route, [], 10, time.monotonic() + 1)['category'] == 'cleanup_pending'
        assert len(spawned) == 1
    finally:
        release.set()
        assert chain._cleanup_finished(str(tmp_path), timeout=2)


_STUBBORN_CHILD = (
    'import signal, runpy; signal.signal(signal.SIGTERM, signal.SIG_IGN); '
    "runpy.run_module('hermes_cli.goal_judge_chain', run_name='__main__')"
)


def _slow_kill(monkeypatch, chain, delay):
    """The reaper's kill lands `delay` s late: a loaded host, or a child that resists termination."""
    import agent.deadline
    real_killpg = getattr(chain.os, 'killpg', None)  # windows-footgun: ok — POSIX-only, None on Windows
    real_tree = agent.deadline.kill_process_tree
    def late(real):
        def kill(*args, **kwargs):
            time.sleep(delay)
            return real(*args, **kwargs)
        return kill
    if real_killpg is not None:
        monkeypatch.setattr(chain.os, 'killpg', late(real_killpg))
    monkeypatch.setattr(agent.deadline, 'kill_process_tree', late(real_tree))


@pytest.mark.slow_test(120, reason='Real fresh-interpreter judge children on two sequential routes')
@pytest.mark.parametrize('reap_delay', [0.0, 0.5, 2.0])
def test_hung_primary_is_killed_and_sol_wins(tmp_path, monkeypatch, endpoint, caplog, reap_delay):
    """Apollo 11-08 12:24-12:57: a timed-out primary's slow reap fenced every later rung (cleanup_pending).

    The primary times out on the chain's own 4 s clock whether or not its child got far enough to
    reach the server, so only the reap and the next rung are under test, never child startup."""
    import logging
    import subprocess
    from hermes_cli import goal_judge_chain as chain
    url, calls, replies = endpoint
    release = threading.Event()
    replies['primary'] = lambda: release.wait(60) and None
    replies['gpt-6.1-sol'] = json.dumps(dict(verdict='done', reason='evidence'))
    home = tmp_path
    route = dict(provider='custom', base_url=url, api_key='test-only')
    (home / 'config.yaml').write_text(json.dumps({'auxiliary': {'goal_judge': dict(
        route, model='primary', timeout=4, total_timeout=240,
        fallback_chain=[dict(route, model=m, timeout=45) for m in ('gpt-6.1-sol', 'grok', 'kimi')])}}),
        encoding='utf-8')
    monkeypatch.setenv('HERMES_HOME', str(home))
    children = []
    actual_popen = subprocess.Popen
    def spawn(argv, *args, **kwargs):
        # 2.0: the primary's child also ignores SIGTERM; only the (late) SIGKILL ends it.
        if reap_delay >= 2:
            argv = [argv[0], '-c', _STUBBORN_CHILD]
        children.append(actual_popen(argv, *args, **kwargs))
        return children[-1]
    monkeypatch.setattr(chain.subprocess, 'Popen', spawn)
    _slow_kill(monkeypatch, chain, reap_delay)
    caplog.set_level(logging.INFO, logger=chain.__name__)
    try:
        # The per-rung ceiling is the judge timeout; without it config `timeout` (the primary's 4 s)
        # caps Sol too, and a slow interpreter start on a loaded runner eats the whole rung.
        result = goals.judge_goal('verify', 'evidence', timeout=60)
    finally:
        release.set()
    # The primary may or may not have reached the server before its deadline; Sol must have.
    assert [m for m in calls if m != 'primary'] == ['gpt-6.1-sol']
    assert result[0] == 'done' and not result[2] and not result[4]
    assert 'goal judge attempt failed: rung=0 provider=custom model=primary category=timeout' in caplog.text
    assert 'goal judge winner: rung=1 provider=custom model=gpt-6.1-sol served_model=gpt-6.1-sol' in caplog.text
    # No leftover socket owner, and no fence left behind for the next judge of this home.
    assert [child.poll() is not None for child in children] == [True, True]
    assert str(home) not in chain._CLEANUPS


def test_own_unreaped_rung_does_not_fence_next_rung_but_fences_others(tmp_path, monkeypatch):
    """Past the reap grace the chain still advances; the stuck reap keeps inhibiting OTHER judges."""
    from hermes_cli import goal_judge_chain as chain
    import subprocess, sys
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setattr(chain, '_REAP_GRACE', 0.1)
    actual_popen = subprocess.Popen
    children = []
    def spawn(argv, *args, **kwargs):
        children.append(actual_popen([sys.executable, '-c', 'import time; time.sleep(60)'], *args, **kwargs))
        return children[-1]
    monkeypatch.setattr(chain.subprocess, 'Popen', spawn)
    _slow_kill(monkeypatch, chain, 1.0)
    routes = [dict(provider='custom', model=m, timeout=0.3) for m in ('primary', 'second')]
    original = chain.run_attempt
    seen = []
    def attempt(route, *args, **kwargs):
        outcome = original(route, *args, **kwargs)
        seen.append((route['model'], outcome['category']))
        if route['model'] == 'second':
            return dict(category='ok', content='{"verdict":"continue","reason":"ran"}')
        return outcome
    monkeypatch.setattr(chain, 'run_attempt', attempt)
    result = chain.judge_chain((routes, 30), 's', 'p', 10, goals._parse_judge_response)
    assert [model for model, _ in seen] == ['primary', 'second'] and len(children) == 2
    assert seen[0][1] == 'cleanup_pending'
    assert result[0] == 'continue' and result[1] == 'ran' and not result[4]
    # A foreign judge for this home is still fenced until the stuck reap lands.
    if str(tmp_path) in chain._CLEANUPS:
        assert original(routes[0], [], 10, time.monotonic() + 1)['category'] == 'cleanup_pending'
        assert len(children) == 2
    assert chain._cleanup_finished(str(tmp_path), timeout=5)
    assert all(child.poll() is not None for child in children)


def test_foreign_pending_cleanup_advances_chain_without_spawning(tmp_path, monkeypatch):
    from hermes_cli import goal_judge_chain as chain
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    foreign = threading.Event()
    chain._CLEANUPS[str(tmp_path)] = {foreign}
    monkeypatch.setattr(chain.subprocess, 'Popen', lambda *a, **kw: pytest.fail('fenced home spawned'))
    routes = [dict(provider='custom', model=m, timeout=1) for m in ('primary', 'second', 'third')]
    try:
        with mock.patch.object(chain.logger, 'info') as info:
            result = chain.judge_chain((routes, 5), 's', 'p', 10, goals._parse_judge_response)
    finally:
        chain._release_cleanup(str(tmp_path), foreign)
    failed = [c.args for c in info.call_args_list if 'attempt failed' in c.args[0]]
    assert [args[1] for args in failed] == [0, 1, 2]
    assert all(args[-1] == 'cleanup_pending' for args in failed)
    assert result[0] == 'continue' and result[4] and 'cleanup_pending' in result[1]


def test_child_relaunch_opt_out_and_stderr_tail(tmp_path, monkeypatch, caplog):
    """ACE-AI 19:5x: outside the gateway unit the child was relaunched and read EMPTY stdin."""
    import logging
    import subprocess, sys
    from hermes_cli import goal_judge_chain as chain
    monkeypatch.delenv('HERMES_DISABLE_LAZY_INSTALLS', raising=False)
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    actual_popen = subprocess.Popen
    envs = []
    def spawn(argv, *args, **kwargs):
        envs.append(kwargs['env'].get('HERMES_DISABLE_LAZY_INSTALLS'))
        # The relaunched child's shape: payload lost, json.load fails, exit 1 with a traceback.
        child = 'import io, json; json.load(io.StringIO(""))'
        return actual_popen([sys.executable, '-c', child], *args, **kwargs)
    monkeypatch.setattr(chain.subprocess, 'Popen', spawn)
    caplog.set_level(logging.WARNING, logger=chain.__name__)
    outcome = chain.run_attempt(dict(provider='custom', model='primary', timeout=10), [], 10, time.monotonic() + 10)
    assert envs == ['1']
    assert outcome['category'] == 'worker_error'
    assert 'goal judge worker_error: returncode=1' in caplog.text and 'JSONDecodeError' in caplog.text


@pytest.mark.parametrize('case', ['long', 'accounting_cancel', 'concurrent', 'storage', 'affinity'])
def test_review_boundary_contracts(tmp_path, monkeypatch, endpoint, case):
    from concurrent.futures import ThreadPoolExecutor
    from agent import auxiliary_client as ac, aux_accounting, portal_tags
    from hermes_cli import goal_judge_chain as chain
    url, calls, replies = endpoint
    configure(tmp_path, url)
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    if case == 'long':
        replies['primary'] = json.dumps(dict(verdict='done', reason='a' * 16000))
        replies['sol'] = '{"verdict":"continue"}'
        result = goals.judge_goal('deliver', 'evidence')
        assert result[0] == 'done' and calls == ['primary']
    elif case == 'accounting_cancel':
        replayed = []
        completed = threading.Event()
        class Worker:
            returncode = 0
            def communicate(self, *args, **kwargs):
                completed.set()
                return json.dumps(dict(category='ok', content='{}', accounting=[{'billable': True}])), None
        monkeypatch.setattr(chain.subprocess, 'Popen', lambda *a, **kw: Worker())
        monkeypatch.setattr(aux_accounting, 'replay_aux_accounting', replayed.extend)
        with pytest.raises(ac.AuxiliaryExplicitCancellation):
            chain.run_attempt({}, [], 10, time.monotonic() + 10, cancel_check=completed.is_set)
        assert replayed == [{'billable': True}]
    elif case == 'concurrent':
        both = threading.Barrier(2, timeout=2)
        class Worker:
            returncode = 0
            def communicate(self, *args, **kwargs):
                both.wait()
                return json.dumps(dict(category='ok', content='{}')), None
        monkeypatch.setattr(chain.subprocess, 'Popen', lambda *a, **kw: Worker())
        def attempt():
            return chain.run_attempt({}, [], 10, time.monotonic() + 5)
        with ThreadPoolExecutor(2) as pool:
            futures = [pool.submit(attempt) for _ in range(2)]
            assert [f.result()['category'] for f in futures] == ['ok', 'ok']
    elif case == 'storage':
        manager = goals.GoalManager('storage-check')
        manager.set('deliver')
        def attempt(*args, cancel_check=None, **kwargs):
            if cancel_check():
                raise ac.AuxiliaryExplicitCancellation()
            return dict(category='ok', content='{"verdict":"continue"}')
        monkeypatch.setattr(chain, 'run_attempt', attempt)
        # A missing DB and a failed committed read are not an explicit stop.
        original_db = goals._get_session_db
        monkeypatch.setattr(goals, '_get_session_db', lambda: None)
        manager.evaluate_after_turn('evidence')
        monkeypatch.setattr(goals, '_get_session_db', original_db)
        db = original_db()
        monkeypatch.setattr(db, 'get_meta_committed', lambda *a: (_ for _ in ()).throw(OSError('read failed')))
        manager.evaluate_after_turn('evidence')
    else:
        config = json.loads((tmp_path / 'config.yaml').read_text(encoding='utf-8-sig'))
        config['custom_providers'] = [{'name': 'session-relay', 'base_url': url,
                                      'api_key': 'test-only', 'session_affinity_header': 'x-session-id'}]
        (tmp_path / 'config.yaml').write_text(json.dumps(config), encoding='utf-8')
        # Inspect the actual request at the HTTP server, not just the IPC payload.
        endpoint_headers = replies['_headers'] = []
        replies['primary'] = '{"verdict":"continue"}'
        for mode, value in [('affinity', 'kanban:task-7'), ('conversation', 'root-7'), ('session', 'session-7')]:
            tokens = (portal_tags.set_affinity_scope(value if mode == 'affinity' else None),
                      portal_tags.set_conversation_context(value if mode == 'conversation' else None))
            try:
                with ac.scoped_runtime_main({'session_id': value if mode == 'session' else ''}):
                    assert goals.judge_goal('deliver', 'evidence')[0] == 'continue'
            finally:
                portal_tags.reset_conversation_context(tokens[1])
                portal_tags.reset_affinity_scope(tokens[0])
        assert endpoint_headers == ['kanban:task-7', 'root-7', 'session-7']
