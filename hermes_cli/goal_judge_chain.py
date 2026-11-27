"""Explicit verdict-chain policy; provider routing remains in auxiliary_client.

Each request has a process owner so deadlines and host cancellation can reap the
actual socket owner, rather than abandoning a billing request on a daemon thread.
"""
from __future__ import annotations

import json
import logging
import math
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

logger = logging.getLogger(__name__)
_LABEL = re.compile(r'[^a-zA-Z0-9_.:/-]')
_CLEANUP_LOCK = threading.Lock()
_CLEANUPS = {}
# A killed rung may take this long to be reaped, drawn from the chain's total budget
# (not the timed-out rung's own, already spent, budget). A loaded host needs more than 50 ms.
_REAP_GRACE = 5.0
_STDERR_TAIL = 300


def _label(value):
    return _LABEL.sub('_', str(value))[:100]


def _positive(value, name):
    if isinstance(value, bool):
        raise ValueError(name)
    value = float(value)
    if not math.isfinite(value) or value <= 0:
        raise ValueError(name)
    return value


def chain_config(timeout):
    from hermes_cli.config import load_config
    config = (load_config().get('auxiliary') or {}).get('goal_judge') or {}
    chain = config.get('fallback_chain')
    if chain is None or chain == []:
        return None
    if not isinstance(chain, list) or len(chain) > 7:
        raise ValueError('fallback_chain must contain at most seven routes')
    timeout = _positive(timeout, 'timeout')
    total = _positive(config.get('total_timeout', timeout), 'total_timeout')
    routes = []
    # Do not inherit primary endpoint, credentials or vendor-specific body into other routes.
    primary = {k: v for k, v in config.items() if k not in ('fallback_chain', 'total_timeout')}
    for entry in [primary, *chain]:
        if not isinstance(entry, dict):
            raise ValueError('route must be a mapping')
        route = dict(entry)
        for key in ('provider', 'model'):
            if not isinstance(route.get(key), str) or not route[key].strip() or route[key] == 'auto':
                raise ValueError('each route requires explicit provider and model')
        route['timeout'] = min(_positive(route.get('timeout', timeout), 'timeout'), timeout)
        routes.append(route)
    return routes, total


def _cleanup_finished(home, timeout=0):
    with _CLEANUP_LOCK:
        pending = tuple(_CLEANUPS.get(home, ()))
    deadline = time.monotonic() + timeout
    return all(event.wait(max(0, deadline - time.monotonic())) for event in pending)


def _release_cleanup(home, finished):
    with _CLEANUP_LOCK:
        pending = _CLEANUPS.get(home)
        if pending is not None:
            pending.discard(finished)
            if not pending:
                _CLEANUPS.pop(home, None)
        finished.set()


def _stop(proc, home, deadline, finished):
    """Retain ownership until reaped; a stuck kill must not hold the judge caller."""
    if proc.returncode is not None:
        # communicate() already reaped this worker; thread scheduling must not
        # turn a completed response into cleanup_pending on a busy host.
        _release_cleanup(home, finished)
        return True
    with _CLEANUP_LOCK:
        _CLEANUPS.setdefault(home, set()).add(finished)
    def reap():
        from agent.deadline import kill_process_tree
        while True:
            try:
                if proc.poll() is None:
                    if os.name == 'posix':
                        import signal
                        # This worker owns a fresh session. Avoid a whole-host
                        # psutil census before terminating its socket owner.
                        os.killpg(proc.pid, signal.SIGKILL)  # windows-footgun: ok — POSIX-only; worker owns this group
                    else:
                        kill_process_tree(proc.pid)
                proc.wait(timeout=0.05)
            except Exception:
                # Keep the process handle (not just a reusable PID) and inhibit
                # further requests for this profile until termination is observed.
                time.sleep(0.05)
                continue
            break
        _release_cleanup(home, finished)

    threading.Thread(target=reap, name='goal-judge-reaper', daemon=True).start()
    stopped = finished.wait(max(0, deadline - time.monotonic()))
    if not stopped:
        logger.warning('goal judge cleanup pending: pid=%d; profile retries inhibited', proc.pid)
    return stopped


def run_attempt(route, messages, max_tokens, deadline, cancel_check=None, *,
                budget_deadline=None, own_cleanups=None):
    """One rung in an owned child. ``own_cleanups`` holds this chain's still-unreaped
    rungs: they do not fence the next rung (it gets its own process); any other
    pending reap for the home does. ``budget_deadline`` bounds the reap grace."""
    from agent.auxiliary_client import raise_if_aux_cancel_requested
    from hermes_constants import get_hermes_home
    from tools.environments.local import served_profile_child_env

    home = str(get_hermes_home())
    raise_if_aux_cancel_requested()
    finished = threading.Event()
    with _CLEANUP_LOCK:
        if _CLEANUPS.get(home, set()) - (own_cleanups or set()):
            return dict(category='cleanup_pending')
    err = None
    try:
        from agent.auxiliary_client import _runtime_main_value
        from agent.portal_tags import get_affinity_scope, get_conversation_context
        env = served_profile_child_env(target_home=get_hermes_home(), inherit_credentials=True)
        env['PYTHONIOENCODING'] = 'utf-8'
        # Outside a gateway unit the launcher's source-update relaunch re-execs this
        # child and the payload on stdin never reaches it. The judge child never owes
        # that tail (and has no budget for it); the gateway unit already sets this.
        env['HERMES_DISABLE_LAZY_INSTALLS'] = '1'
        payload = json.dumps(dict(route=route, messages=messages, max_tokens=max_tokens,
                                  affinity_scope=get_affinity_scope(), conversation=get_conversation_context(),
                                  session_id=_runtime_main_value('session_id'),
                                  cache_scope=_runtime_main_value('cache_scope')))
        # -m uses THIS source checkout, not the installed runtime's editable root.
        # stderr goes to an unlinked file, read only to explain a worker_error.
        err = tempfile.TemporaryFile()
        proc = subprocess.Popen(
            [sys.executable, '-m', 'hermes_cli.goal_judge_chain'],
            cwd=str(Path(__file__).resolve().parent.parent), env=env,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=err,
            text=True, encoding='utf-8',
            start_new_session=os.name == 'posix',
        )
    except (OSError, ValueError, TypeError):
        if err is not None:
            err.close()
        return dict(category='worker_error')
    outcome = dict(category='timeout')
    graceful = False
    try:
        pending = payload
        next_state_check = 0.0
        while True:
            raise_if_aux_cancel_requested()
            if cancel_check is not None and time.monotonic() >= next_state_check:
                next_state_check = time.monotonic() + 0.2
                if cancel_check():
                    from agent.auxiliary_client import AuxiliaryExplicitCancellation
                    raise AuxiliaryExplicitCancellation()
            # Reserve a small part of the attempt's own budget for normal reaping.
            remaining = deadline - time.monotonic() - 0.05
            if remaining <= 0:
                break
            try:
                out, _ = proc.communicate(pending, timeout=min(0.05, remaining))
                if proc.returncode:
                    outcome = dict(category='worker_error')
                else:
                    outcome = json.loads(out)
                    if not isinstance(outcome, dict) or outcome.get('category') not in {
                        'ok', 'unavailable', 'timeout', 'invalid_output', 'configuration', 'request_error',
                    } or (outcome.get('category') == 'ok' and not isinstance(outcome.get('content'), str)):
                        outcome = dict(category='worker_error')
                from agent.aux_accounting import replay_aux_accounting
                replay_aux_accounting(outcome.pop('accounting', []))
                raise_if_aux_cancel_requested()
                if cancel_check is not None and cancel_check():
                    from agent.auxiliary_client import AuxiliaryExplicitCancellation
                    raise AuxiliaryExplicitCancellation()
                break
            except subprocess.TimeoutExpired:
                pending = None
        graceful = True
    except (OSError, ValueError, UnicodeError):
        outcome = dict(category='worker_error')
        graceful = True
    finally:
        now = time.monotonic()
        # Explicit cancel (or any escaping exception) keeps the old 50 ms bound and
        # propagates at once; a finished rung may spend part of the chain budget on reaping.
        if graceful and budget_deadline is not None:
            until = min(budget_deadline, now + _REAP_GRACE)
        else:
            until = min(deadline, now + 0.05)
        stopped = _stop(proc, home, until, finished)
        if not stopped and own_cleanups is not None:
            own_cleanups.add(finished)
        if graceful and outcome.get('category') == 'worker_error':
            logger.warning('goal judge worker_error: returncode=%s stderr_tail=%r',
                           proc.returncode, _tail(err))
        err.close()
    return outcome if stopped else dict(category='cleanup_pending')


def _tail(stream):
    try:
        stream.seek(0, os.SEEK_END)
        stream.seek(max(0, stream.tell() - _STDERR_TAIL))
        return ' '.join(stream.read().decode('utf-8', 'replace').split())
    except (OSError, ValueError, AttributeError):
        return ''


def judge_chain(policy, system_prompt, prompt, max_tokens, parse, cancel_check=None):
    from agent.auxiliary_client import raise_if_aux_cancel_requested
    routes, total = policy
    deadline = time.monotonic() + total
    messages = [dict(role='system', content=system_prompt), dict(role='user', content=prompt)]
    last_category = 'unavailable'
    own_cleanups = set()  # this chain's killed-but-unreaped rungs; still fence other judges
    for index, route in enumerate(routes):
        raise_if_aux_cancel_requested()
        if time.monotonic() >= deadline:
            last_category = 'total_timeout'
            break
        provider, model = _label(route['provider']), _label(route['model'])
        logger.info('goal judge attempt: rung=%d provider=%s model=%s', index, provider, model)
        outcome = run_attempt(route, messages, max_tokens,
                              min(deadline, time.monotonic() + route['timeout']), cancel_check=cancel_check,
                              budget_deadline=deadline, own_cleanups=own_cleanups)
        category = outcome.get('category', 'worker_error')
        if category == 'ok':
            verdict, reason, failed, directive = parse(outcome.get('content', ''))
            if not failed:
                logger.info('goal judge winner: rung=%d provider=%s model=%s served_model=%s',
                            index, provider, model, _label(outcome.get('served_model', 'unknown')))
                return verdict, reason, False, directive, False
            category = 'invalid_output'
        logger.info('goal judge attempt failed: rung=%d provider=%s model=%s category=%s',
                    index, provider, model, category)
        last_category = category
        # cleanup_pending: a reap still owed (ours, or another judge's for this home) is
        # an unavailable rung, not a verdict; the next rung runs while budget remains.
        if category not in {'unavailable', 'timeout', 'invalid_output', 'configuration', 'worker_error',
                            'cleanup_pending'}:
            break
    return 'continue', f'goal judge chain failed: {last_category}', last_category == 'invalid_output', None, last_category != 'invalid_output'


def _worker(payload):
    from agent.aux_accounting import capture_aux_accounting
    from agent.auxiliary_client import scoped_runtime_main
    from agent.portal_tags import (
        set_affinity_scope, reset_affinity_scope, set_conversation_context, reset_conversation_context,
    )
    affinity = set_affinity_scope(payload.get('affinity_scope'))
    conversation = set_conversation_context(payload.get('conversation'))
    try:
        with scoped_runtime_main({key: payload.get(key) for key in ('session_id', 'cache_scope')}):
            with capture_aux_accounting() as accounting:
                result = _worker_result(payload)
    finally:
        reset_conversation_context(conversation)
        reset_affinity_scope(affinity)
    result['accounting'] = accounting
    return result


def _worker_result(payload):
    from agent.auxiliary_attempt import single_attempt
    from agent.auxiliary_client import call_llm
    from agent.auxiliary_unavailable import AuxiliaryClientUnavailable
    route = payload['route']
    try:
        with single_attempt('goal_judge', route):
            response = call_llm(task='goal_judge', messages=payload['messages'], temperature=0,
                                max_tokens=payload['max_tokens'], timeout=route['timeout'])
        content = response.choices[0].message.content or ''
        return dict(category='ok', content=content, served_model=getattr(response, 'model', '') or '')
    except AuxiliaryClientUnavailable:
        return dict(category='unavailable')
    except Exception as exc:
        from agent.auxiliary_client import (
            _is_transient_transport_error, _is_rate_limit_error, _is_auth_error,
            _is_invalid_aux_response_error,
        )
        if isinstance(exc, TimeoutError):
            category = 'timeout'
        elif _is_invalid_aux_response_error(exc):
            category = 'invalid_output'
        elif _is_transient_transport_error(exc) or _is_rate_limit_error(exc) or _is_auth_error(exc):
            category = 'unavailable'
        elif isinstance(exc, (ValueError, KeyError)) or getattr(exc, 'status_code', None) == 404:
            category = 'configuration'
        else:
            category = 'request_error'
        return dict(category=category)


if __name__ == '__main__':
    # stdout is an internal IPC channel; plugin/library prints must not corrupt it.
    import contextlib
    logging.disable(logging.CRITICAL)
    with contextlib.redirect_stdout(sys.stderr):
        result = _worker(json.load(sys.stdin))
    print(json.dumps(result))
