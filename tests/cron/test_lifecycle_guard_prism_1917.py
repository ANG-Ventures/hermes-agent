"""Prism P1 findings on #1917 @ e3e2257f (card t_9c269c34): every shape below was ALLOWED on
e3e2257f. Most were blocked on its parent; the wrapped-pipe Branch-A forms were open on both.

Deny-shaped literals are assembled at runtime so reading this file through an agent tool does
not trip the gates it tests.
"""

from __future__ import annotations

import pytest

from cron.lifecycle_guard import scan_gateway_lifecycle

LABEL = "ai.hermes." + "gateway-zeus"
UNIT = "hermes-" + "gateway-zeus"
BOOTOUT = "launch" + "ctl boot" + "out gui/502/" + LABEL
SYSTEMCTL = "systemctl --user " + "st" + "op " + UNIT
RESTART = "hermes gateway " + "re" + "start"
STOP = "hermes gateway " + "st" + "op"


@pytest.fixture(autouse=True)
def _own_gateway(monkeypatch):
    import cron.lifecycle_guard as lifecycle_guard

    monkeypatch.setenv("XPC_SERVICE_NAME", LABEL)
    monkeypatch.delenv("INVOCATION_ID", raising=False)
    monkeypatch.setattr(lifecycle_guard, "_profile_derived_self_names", lambda: {LABEL, UNIT})


def _blocked(command: str) -> bool:
    unsafe, refusal = scan_gateway_lifecycle(command, cwd="/tmp")
    assert refusal is None, refusal
    return unsafe


@pytest.mark.parametrize("command", [
    # 46c2adba debugger
    f"gdb --batch -ex 'shell {RESTART}'",
    # 7da58e26 loop word list
    f"for c in '{RESTART}'; do $c; done",
    # 2ce0f2f0 / 260b9350 process selectors and deferred builtins
    "pkill -f 'hermes " + "gateway'",
    'pkill -f "hermes ' + 'gateway"',
    f"trap '{STOP}' EXIT",
    # 7bdfbaaa wrapped pipe executors
    f"echo '{BOOTOUT}' | env bash",
    f"echo '{BOOTOUT}' | sudo -u root bash",
    f"echo '{BOOTOUT}' | sudo -E sh",
    f"echo '{BOOTOUT}' | command bash",
    f"echo '{BOOTOUT}' | nohup sh",
    f"echo '{BOOTOUT}' | timeout 5 bash",
    f"echo '{BOOTOUT}' | exec bash",
    f"echo '{BOOTOUT}' | ssh localhost",
    f"echo '{RESTART}' | env bash",
    f"grep -r '{RESTART}' . | env bash",
    f"cat <<EOF | env bash\n{RESTART}\nEOF",
    # a11dd2c0 stored values
    f'export C="{SYSTEMCTL}"; $C',
    f'declare C="{SYSTEMCTL}"; $C',
    f'readonly C="{SYSTEMCTL}"; $C',
    f'typeset C="{SYSTEMCTL}"; $C',
    f'set -- "{BOOTOUT}"; $1',
    f'f(){{ $@; }}; f "{SYSTEMCTL}"',
    # same class, stored on disk (found by operator probe, not by Prism)
    f"git config alias.x '!{RESTART}'; git x",
    f"git config core.pager '{RESTART}'; git log",
    # 31197de5 pipeline continued on the next line
    f"echo '{SYSTEMCTL}' |\nbash",
])
def test_quoted_value_that_still_executes_blocks(command):
    assert _blocked(command)


def test_noexec_reenabled_by_plus_o_blocks(tmp_path):
    """1fc8d7bd: `+o noexec` clears `-n`; the written script runs."""
    new = tmp_path / "new.sh"
    assert _blocked(f"printf '{BOOTOUT}' > {new}; bash -n +o noexec {new}")


def test_python_variable_path_write_then_run_blocks(tmp_path):
    """e63acd19: open() on a variable path is an untracked write."""
    bounce = tmp_path / "bounce.sh"
    bounce.write_text("echo harmless\n", encoding="utf-8")
    body = f'p = "{bounce}"\nf = open(p, "w")\nf.write("{RESTART}\\n")\n'
    assert _blocked(f"python3 - <<'PY'\n{body}PY\nbash {bounce}")


@pytest.mark.parametrize("command", [
    # 52fa2744 shell startup file, executed without being named
    f"python3 - <<'PY'\nopen('/Users/x/.bashrc', 'a').write('{RESTART}\\n')\nPY\nbash -i -c ':'",
    # b963b1c8 the body writes and runs the file itself
    f"python3 - <<'PY'\nimport subprocess\nopen('/tmp/x.sh','w').write('{BOOTOUT}')\nsubprocess.run(['bash','/tmp/x.sh'])\nPY",
])
def test_python_heredoc_write_then_execute_blocks(command):
    assert _blocked(command)


def test_sqlite_shell_escape_in_memory_blocks():
    """e95c8e91: already blocked on e3e2257f; pinned."""
    assert _blocked(f"sqlite3 :memory: '.shell {RESTART}'")


@pytest.mark.parametrize("command", [
    # the allowlisted content commands keep the #1917 false-positive fix
    f"gh pr comment 12 --body 'never {RESTART}'",
    f"hermes kanban complete t_x --summary 'avoid {RESTART} here'",
    f"git commit -m 'docs: {RESTART} from a shell'",
    # a pipe into a non-executor (also continued across lines) is not an executor feed
    f"echo '{BOOTOUT}' | grep x | wc -l",
    f"echo '{RESTART}' |\nwc -l",
    # `bash -n` with no re-enabling option still skips its operand
    f"bash -n /tmp/x.sh && echo '{RESTART}'",
])
def test_content_arguments_still_pass(command):
    assert not _blocked(command)
