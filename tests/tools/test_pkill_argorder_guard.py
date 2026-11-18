"""pkill/pgrep argument-order guard (t_c6d2e782).

BSD pkill (macOS) reads every word after the pattern as another pattern, so the
11-17 ``sudo -n pkill -f '^/usr/bin/eslogger exec$' -P 60886`` matched every
process with ``-P`` in its argv: all kanban workers (``python -P``) and every
terminal-tool shell (``pwd -P``). Both incident spellings must be refused, on the
command line and in a written shell script; options-first spellings must pass.
"""

import json
import sys

import pytest

from tools.pkill_argorder_guard import find_misordered_pkill
from tools.terminal_tool_guards import pkill_argorder_block
from tools.file_tools_write_guards import _check_pkill_argorder_script

# Verbatim from the exec flight recorder, 2026-11-17 11:06:16 (zsh pid 61140).
EVENT_1106 = (
    "cd /Volumes/fleet-scratch/tmp/t_1d5ff6b8; rm -f es.jsonl; sudo -n /usr/bin/eslogger exec > es.jsonl "
    "2>es.err & sleep 8; ls -la es.jsonl; sleep 2; "
    "sudo -n pkill -f '^/usr/bin/eslogger exec$' -P $(pgrep -f 'sudo -n /usr/bin/eslogger exec' | head -1)"
)
# Last line of /Volumes/fleet-scratch/tmp/t_1d5ff6b8/t1d5_ab.sh, run 2026-11-17 20:56:34.
EVENT_2057_SCRIPT = (
    "#!/usr/bin/env bash\n"  # original used a fixed bash path; the guard keys on any sh shebang
    "sudo -n /usr/bin/eslogger exec > \"$T/es.fifo\" 2>/dev/null & ES=$!\n"
    "wait $PY; sudo -n /bin/kill $(pgrep -P $ES) $ES 2>/dev/null; "
    "sudo -n /usr/bin/pkill -f \"^/usr/bin/eslogger exec$\" -P $ES\n"
)


@pytest.mark.parametrize("command", [
    EVENT_1106,
    "ssh -n studio " + json.dumps(EVENT_1106),          # shipped to another host
    "bash -c 'pkill -f worker -U bob'",
    "pgrep -f foo -l",
    "sudo -u bob pkill -f worker -U bob",
])
def test_misordered_spellings_are_found(command):
    assert find_misordered_pkill(command)


@pytest.mark.parametrize("command", [
    "sudo -n pkill -P 60886 -f '^/usr/bin/eslogger exec$'",
    "sudo -n /usr/bin/pkill -x eslogger; date",
    "pkill -9 -f imgw10.d",
    "pkill -f -- -P",
    "kill $(pgrep -P $ES) $ES",
    "sudo -u bob pkill -U bob -f worker",
    "pgrep -lf eslogger",
    "grep pkill notes.txt -n",
    "nice -n 5 pkill -f x",
    "echo hello",
])
def test_options_first_and_unrelated_commands_pass(command):
    assert find_misordered_pkill(command) is None


def test_terminal_guard_refuses_the_1106_command():
    out = pkill_argorder_block(command=EVENT_1106)
    assert out is not None
    payload = json.loads(out)
    assert payload["status"] == "blocked"
    assert "-P" in payload["error"] and "before the pattern" in payload["error"]
    assert pkill_argorder_block(command="sudo -n pkill -P 60886 -f '^/usr/bin/eslogger exec$'") is None


def test_write_guard_refuses_the_2057_script_but_not_prose():
    assert _check_pkill_argorder_script("/tmp/t1d5_ab.sh", EVENT_2057_SCRIPT)
    assert _check_pkill_argorder_script("/tmp/runner", EVENT_2057_SCRIPT)  # shebang, no suffix
    # A note that quotes the bad line is not a script.
    assert _check_pkill_argorder_script("/tmp/notes.md", EVENT_2057_SCRIPT.split("\n", 1)[1]) is None
    fixed = EVENT_2057_SCRIPT.replace('pkill -f "^/usr/bin/eslogger exec$" -P $ES',
                                      'pkill -P $ES -f "^/usr/bin/eslogger exec$"')
    assert _check_pkill_argorder_script("/tmp/t1d5_ab.sh", fixed) is None


@pytest.mark.skipif(sys.platform != "darwin", reason="the trap is BSD pgrep argv parsing")
def test_bsd_pgrep_really_treats_trailing_options_as_patterns(tmp_path):
    """The premise, on the real tool: ``pgrep -f NOMATCH -P <none>`` lists processes with ``-P`` in argv."""
    import subprocess

    sleeper = subprocess.Popen(["/bin/sh", "-c", "sleep 30; :", "dummy", "-P"])
    try:
        bad = subprocess.run(["pgrep", "-f", "^no-such-process$", "-P", "999999"],
                             capture_output=True, text=True).stdout.split()
        good = subprocess.run(["pgrep", "-P", "999999", "-f", "^no-such-process$"],
                              capture_output=True, text=True).stdout.split()
    finally:
        sleeper.kill()
        sleeper.wait()
    assert str(sleeper.pid) in bad
    assert good == []
