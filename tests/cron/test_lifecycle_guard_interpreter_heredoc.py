"""Lifecycle guard: a Python/osascript heredoc that SPAWNS a lifecycle command is blocked.

A quoted-delimiter heredoc owned by a non-shell interpreter is masked as shell-inert data, which is
right for prose and for code that only mentions the phrase -- but the interpreter executes its body,
so a body that spawns a process can restart or kill the gateway from inside the heredoc.
"""
import pytest

from cron.lifecycle_guard import contains_gateway_lifecycle_command_or_referenced_script as blocked

LABEL = "gui/501/ai.hermes.gateway"


def _py(body, interp="python3 -", delim="PY"):
    return f"{interp} <<'{delim}'\n{body}\n{delim}"


@pytest.mark.parametrize("command", [
    _py(f"import subprocess\nsubprocess.run(['launchctl', 'kickstart', '-k', '{LABEL}'])"),
    _py("import os\nos.system('hermes gateway restart')"),
    _py("import subprocess\nsubprocess.Popen('hermes gateway stop', shell=True)"),
    _py("from os import system\nsystem('hermes gateway restart')"),
    _py("import os\nos.execvp('hermes', ['hermes', 'gateway', 'restart'])"),
    _py("import subprocess as sp\nsp.check_call(['pkill', '-f', 'hermes gateway'])"),
    _py("import asyncio\nasyncio.run(asyncio.create_subprocess_exec('hermes', 'gateway', 'restart'))"),
    _py("exec(\"import os; os.system('hermes gateway restart')\")"),
    _py("import subprocess\nsubprocess.run(['hermes', 'gateway', 'restart'])", interp="/usr/bin/python3.11"),
    _py('do shell script "hermes gateway restart"', interp="osascript", delim="OSA"),
    _py("import subprocess\nsubprocess.run(['hermes','gateway','restart'])", interp="env FOO=1 python3"),
])
def test_interpreter_heredoc_that_spawns_a_lifecycle_command_is_blocked(command):
    assert blocked(command), command


@pytest.mark.parametrize("command", [
    _py("print(open('/tmp/gateway.log').read().count('hermes gateway restart'))"),
    _py("import json\nprint(json.dumps({'hint': 'run hermes gateway restart from a shell'}))"),
    _py("import subprocess\nprint(subprocess.run(['git', 'status'], capture_output=True).stdout)"),
    "cat > notes.md <<'EOF'\nRun hermes gateway restart after deploy.\nEOF",
])
def test_data_only_or_unrelated_bodies_are_allowed(command):
    assert not blocked(command), command
