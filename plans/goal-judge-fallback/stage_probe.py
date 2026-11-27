"""Diagnostic plugin: monotonic worker stages, no prompts, credentials or verdict text.

Load with -p plans.goal-judge-fallback.stage_probe via importlib path instead of
shipping a production tracing knob. Set JUDGE_STAGE_TRACE to a private JSONL path.
"""
import json
import os
import time


def _event(stage, **fields):
    path = os.environ.get('JUDGE_STAGE_TRACE')
    if path:
        with open(path, 'a', encoding='utf-8') as stream:
            stream.write(json.dumps(dict(stage=stage, pid=os.getpid(), at=time.monotonic(), **fields)) + '\n')


_CHILD = '''
import builtins, json, os, runpy, time
p = os.environ['JUDGE_STAGE_TRACE']
def event(stage):
    with open(p, 'a', encoding='utf-8') as f:
        f.write(json.dumps(dict(stage=stage, pid=os.getpid(), at=time.monotonic())) + '\\n')
event('child_start')
original = builtins.__import__
def traced(name, *args, **kwargs):
    tracked = name == 'agent.auxiliary_client'
    if tracked: event('auxiliary_import_begin')
    result = original(name, *args, **kwargs)
    if tracked: event('auxiliary_import_end')
    return result
builtins.__import__ = traced
runpy.run_module('hermes_cli.goal_judge_chain', run_name='__main__')
event('child_result_written')
'''


def pytest_sessionstart(session):
    import subprocess
    from hermes_cli import goal_judge_chain as chain
    original_popen = subprocess.Popen
    original_stop = chain._stop
    original_attempt = chain.run_attempt
    import agent.deadline
    original_kill = agent.deadline.kill_process_tree

    def kill(pid, **kwargs):
        _event('kill_begin', child=pid)
        try:
            result = original_kill(pid, **kwargs)
            _event('kill_return', child=pid, signalled=result)
            return result
        except Exception as exc:
            _event('kill_error', child=pid, error_type=type(exc).__name__, error=str(exc)[:300])
            raise

    agent.deadline.kill_process_tree = kill

    class TracedPopen(original_popen):
        def __init__(self, argv, *args, **kwargs):
            traced = argv[1:] == ['-m', 'hermes_cli.goal_judge_chain']
            if traced:
                kwargs['env']['JUDGE_STAGE_TRACE'] = os.environ['JUDGE_STAGE_TRACE']
                argv = [argv[0], '-c', _CHILD]
                _event('spawn_begin')
            super().__init__(argv, *args, **kwargs)
            if traced:
                _event('spawn_end', child=self.pid)

    def stop(proc, home, deadline, finished):
        _event('terminate_begin', child=proc.pid, returncode=proc.returncode)
        result = original_stop(proc, home, deadline, finished)
        _event('reap_return', child=proc.pid, stopped=result, returncode=proc.returncode)
        if not result:
            import sys
            import traceback
            for tid, frame in sys._current_frames().items():
                _event('cleanup_stack', thread=tid, stack=[f'{f.filename}:{f.lineno}:{f.name}' for f in traceback.extract_stack(frame)])
        return result

    def attempt(route, *args, **kwargs):
        _event('attempt_begin', model=route['model'])
        result = original_attempt(route, *args, **kwargs)
        _event('attempt_result', category=result.get('category'))
        return result

    subprocess.Popen = TracedPopen
    chain._stop = stop
    chain.run_attempt = attempt
    # HTTP handling begins before the fixture reads the model or emits a verdict.
    from http.server import ThreadingHTTPServer
    original_finish = ThreadingHTTPServer.finish_request

    def finish(server, *args, **kwargs):
        _event('http_arrival')
        try:
            return original_finish(server, *args, **kwargs)
        finally:
            _event('http_reply_written')

    ThreadingHTTPServer.finish_request = finish
