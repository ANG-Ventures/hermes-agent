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
    # b74a2fa3 (Apollo r1 on #1954): a pipe into a GROUPED executor; every command in the
    # group reads the pipe
    f"echo '{BOOTOUT}' | (bash)",
    f"echo '{BOOTOUT}' | {{ bash; }}",
    f"echo '{BOOTOUT}' | ( env bash )",
    f"echo '{BOOTOUT}' | (cd /tmp; bash)",
    f"echo '{BOOTOUT}' | {{ cd /tmp; bash; }}",
    f"echo '{BOOTOUT}' | ( ( sudo -u root sh ) )",
    f"echo '{BOOTOUT}' |\n(bash)",
    f"echo '{RESTART}' | (sh)",
    # ee373214: `printf -vNAME` (attached) stores the value exactly like `printf -v NAME`
    f"printf -vX '%s' '{BOOTOUT}'; eval \"$X\"",
    f"printf -vX -- '%s' '{BOOTOUT}'; eval \"$X\"",
    f"printf -vX '%s' '{RESTART}'; $X",
])
def test_grouped_pipe_executor_and_attached_printf_v_block(command):
    assert _blocked(command)


@pytest.mark.parametrize("command", [
    # a group that only reads the pipe is not an executor feed
    f"echo '{BOOTOUT}' | (cat)",
    f"echo '{BOOTOUT}' | (cat; wc -l)",
    f"grep '{LABEL}' /tmp/f | {{ head -1; }}",
])
def test_grouped_pipe_reader_still_passes(command):
    assert not _blocked(command)


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


@pytest.mark.parametrize("command", [
    # b74a2fa3 r2: a redirection before the executor word in a pipe stage
    f"echo '{BOOTOUT}' | 2>/dev/null bash",
    f"echo '{BOOTOUT}' | > /tmp/o bash",
    f"echo '{BOOTOUT}' | 2> /tmp/err env bash",
    f"echo '{BOOTOUT}' | (2>/dev/null bash)",
    # a3c0cfc0: heredoc whose `|` continues past the terminator
    f"cat <<'EOF' |\n{BOOTOUT}\nEOF\nbash",
    f"cat <<EOF |\n{RESTART}\nEOF\nenv sh",
    # 7f7a1d3f: executor in the body of a compound command after a pipe
    f"echo '{BOOTOUT}' | if true; then bash; fi",
    f"echo '{BOOTOUT}' | if false; then :; else sh; fi",
    f"echo '{BOOTOUT}' | until false; do bash; break; done",
    f"echo '{BOOTOUT}' | case x in x) bash;; esac",
    # 5d269848: a comment does not close a pipe group
    f"echo '{BOOTOUT}' | {{ # }}\n bash; }}",
    f"echo '{BOOTOUT}' | ( # )\n bash )",
])
def test_pipe_stage_parser_r2_shapes_block(command):
    assert _blocked(command)


@pytest.mark.parametrize("command", [
    # compounds / redirects / comments that never reach an executor stay allowed
    f"echo '{LABEL}' | if true; then cat; fi",
    f"echo '{LABEL}' | 2>/dev/null grep x",
    f"grep '{LABEL}' /tmp/f | {{ # bash\n head -1; }}",
    f"cat <<'EOF' |\n{LABEL}\nEOF\nwc -l",
])
def test_pipe_stage_parser_r2_controls_pass(command):
    assert not _blocked(command)
