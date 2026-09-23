"""Python heredocs and Python files must get the same lifecycle verdict."""

import pytest

from cron.lifecycle_guard import (
    GatewayLifecycleBlocked,
    _direct_lifecycle_scan,
    check_gateway_lifecycle,
    contains_gateway_lifecycle_command_or_referenced_script as guard,
)


def _forms(tmp_path, body):
    script = tmp_path / "diagnose.py"
    script.write_text(body)
    return f"python3 - <<'PY'\n{body}PY", f"python3 {script}"


def test_read_only_python_heredoc_does_not_execute_the_log_it_reads(tmp_path):
    # The log is small and scannable; the old shell-script walk followed this
    # Python string as an executable reference and blocked on its contents.
    log = tmp_path / "intake.jsonl"
    log.write_text("hermes gateway " + "re" + "start\n")
    body = (
        "import subprocess\nfrom pathlib import Path\n"
        "result = subprocess.run(['sudo', '-n', 'sqlite3', "
        f"'file:{tmp_path / 'ledger.sqlite3'}?mode=ro', 'select count(*) from reviews'], "
        "capture_output=True)\n"
        f"print(Path('{log}').read_text())\n"
    )
    heredoc, file_command = _forms(tmp_path, body)
    assert not _direct_lifecycle_scan(body)
    assert not guard(file_command, cwd=str(tmp_path))
    assert not guard(heredoc, cwd=str(tmp_path))


def test_chained_sql_reads_and_original_open_loop_match_file_verdict(tmp_path):
    log = tmp_path / "intake.jsonl"
    log.write_text("hermes gateway " + "restart\n")
    body = (
        "import json,subprocess\n"
        "rows=subprocess.run(['sudo','-n','sqlite3',"
        f"'file:{tmp_path / 'reviews.sqlite3'}?mode=ro','select count(*) from reviews;'],"
        "capture_output=True,text=True).stdout.split()\n"
        f"for line in reversed(open('{log}').read().splitlines()):\n"
        "    try: d=json.loads(line)\n"
        "    except: continue\n"
        "    if d.get('type')!='intake': continue\n"
        "    k=f\"{d['repo'].lower()}#{d['pr']}\"\n"
        "    print(d)\n"
    )
    heredoc, file_command = _forms(tmp_path, body)
    chain = (
        f"DB={tmp_path / 'reviews.sqlite3'}; echo '== running now'; "
        "echo configured | grep configured; "
        'sudo -n sqlite3 "file:$DB?mode=ro" "select count(*) from reviews;"; '
    )
    assert not guard(chain, cwd=str(tmp_path))
    assert not guard(file_command, cwd=str(tmp_path))
    assert not guard(chain + heredoc, cwd=str(tmp_path))
    assert not guard(heredoc, cwd=str(tmp_path))


@pytest.mark.parametrize("action", [
    "subprocess.run(['launchctl', 'bootout', 'system/ai.hermes.gateway'])",
    "subprocess.run(['kill', '$(pgrep -f hermes-gateway)'])",
    "subprocess.run(['systemctl', 'stop', 'hermes-gateway'])",
    "__import__('os').system('/tmp/restart.sh')",
])
def test_chained_python_heredoc_keeps_executable_actions_visible(tmp_path, action):
    script = tmp_path / "restart.sh"
    script.write_text("hermes gateway " + "restart\n")
    body = f"import subprocess\n{action.replace('/tmp/restart.sh', str(script))}\n"
    heredoc, _ = _forms(tmp_path, body)
    assert guard("echo ok; " + heredoc, cwd=str(tmp_path))


def test_read_log_as_executable_python_remains_blocked(tmp_path):
    log = tmp_path / "intake.jsonl"
    log.write_text("hermes gateway " + "restart\n")
    heredoc, _ = _forms(tmp_path, f"exec(open('{log}').read())\n")
    assert guard("echo ok; " + heredoc, cwd=str(tmp_path))


def test_read_log_loop_executing_lines_remains_blocked(tmp_path):
    log = tmp_path / "intake.jsonl"
    log.write_text("hermes gateway " + "restart\n")
    body = (
        f"for line in reversed(open('{log}').read().splitlines()):\n"
        "    __import__('os').system(line)\n"
    )
    heredoc, _ = _forms(tmp_path, body)
    assert guard("echo ok; " + heredoc, cwd=str(tmp_path))


@pytest.mark.parametrize(
    "body",
    [
        "import subprocess\nsubprocess.run(['launchctl', 'bootout', 'system/ai.hermes.gateway'])\n",
        "import subprocess\nsubprocess.run(['kill', '$(pgrep -f hermes-gateway)'])\n",
        "import subprocess\nsubprocess.run(['systemctl', 'stop', 'hermes-gateway'])\n",
    ],
)
def test_python_heredoc_lifecycle_operations_still_block(tmp_path, body):
    heredoc, file_command = _forms(tmp_path, body)
    assert guard(heredoc, cwd=str(tmp_path))
    with pytest.raises(GatewayLifecycleBlocked):
        check_gateway_lifecycle("diagnose", script=file_command.removeprefix("python3 "))


def test_sibling_gateway_in_python_heredoc_uses_label_aware_rules(monkeypatch, tmp_path):
    monkeypatch.setenv("XPC_SERVICE_NAME", "ai.hermes.gateway-aegis")
    body = "import subprocess\nsubprocess.run(['launchctl', 'bootout', 'system/ai.hermes.gateway'])\n"
    heredoc, _ = _forms(tmp_path, body)
    assert not guard(heredoc, cwd=str(tmp_path))


def test_python_heredoc_executing_referenced_script_stays_blocked(tmp_path):
    script = tmp_path / "restart.sh"
    script.write_text("hermes gateway " + "re" + "start\n")
    body = f"import os\nos.system('{script}')\n"
    heredoc, _ = _forms(tmp_path, body)
    assert guard(heredoc, cwd=str(tmp_path))


def test_shadowed_path_name_cannot_hide_executed_script(tmp_path):
    script = tmp_path / "restart.sh"
    script.write_text("hermes gateway " + "re" + "start\n")
    body = (
        "from pathlib import Path\n"
        "Path = lambda value: __import__('os').system(value)\n"
        f"Path('{script}').read_text()\n"
    )
    heredoc, _ = _forms(tmp_path, body)
    assert guard(heredoc, cwd=str(tmp_path))


def test_read_text_piped_to_os_system_is_executable(tmp_path):
    data = tmp_path / "commands.txt"
    data.write_text("hermes gateway " + "re" + "start\n")
    body = f"from pathlib import Path\nimport os\nos.system(Path('{data}').read_text())\n"
    heredoc, _ = _forms(tmp_path, body)
    assert guard(heredoc, cwd=str(tmp_path))


def test_shell_heredoc_owner_not_following_python(tmp_path):
    script = tmp_path / "action.sh"
    script.write_text("hermes gateway " + "restart\n")
    shell = f"bash <<'EOF'; python3 -V\nsh -c '{script}'\nEOF"
    document = "cat <<'EOF' > notes.md; python3 -V\nhermes gateway " + "restart\nEOF"
    assert guard(shell, cwd=str(tmp_path))
    assert not guard(document, cwd=str(tmp_path))


def test_read_only_loop_rebound_parser_must_not_hide_log(tmp_path):
    log = tmp_path / "intake.jsonl"
    log.write_text("hermes gateway " + "restart\n")
    loop = (
        f"for line in reversed(open('{log}').read().splitlines()):\n"
        "    try: d=json.loads(line)\n"
        "    except: continue\n"
        "    print(d)\n"
    )
    ordinary, _ = _forms(tmp_path, "import json\n" + loop)
    rebound, _ = _forms(tmp_path, "import json\njson.loads = print\n" + loop)
    assert not guard(ordinary, cwd=str(tmp_path))
    assert guard(rebound, cwd=str(tmp_path))


def test_read_only_loop_parser_variable_name_is_irrelevant(tmp_path):
    log = tmp_path / "intake.jsonl"
    log.write_text("hermes gateway " + "restart\n")
    body = (
        "import json\n"
        f"for line in reversed(open('{log}').read().splitlines()):\n"
        "    try: rec=json.loads(line)\n"
        "    except: continue\n"
        "    if rec.get('type')!='intake': continue\n"
        "    print(rec['repo'].lower())\n"
    )
    heredoc, file_command = _forms(tmp_path, body)
    assert not guard(file_command, cwd=str(tmp_path))
    assert not guard(heredoc, cwd=str(tmp_path))


@pytest.mark.parametrize("prefix,inside", [
    ("json.loads = print\n", "    print(d)\n"),
    ("setattr(json, 'loads', print)\n", "    print(d)\n"),
    ("print = len\n", "    print(line)\n"),
    ("len = print\n", "    len(line)\n"),
    ("reversed = print\n", "    print(d)\n"),
    ("open = print\n", "    print(d)\n"),
    ("seen = type('Reader', (), {'add': print})()\n", "    seen.add(line)\n"),
    ("out = type('Reader', (), {'append': print})()\n", "    out.append(line)\n"),
    ("", "    d = type('Reader', (), {'get': print})()\n    d.get(line)\n"),
])
def test_read_only_exemption_refuses_rebound_callables(tmp_path, prefix, inside):
    log = tmp_path / "intake.jsonl"
    log.write_text("hermes gateway " + "restart\n")
    body = (
        "import json\n" + prefix
        + f"for line in reversed(open('{log}').read().splitlines()):\n"
        + "    d=json.loads(line)\n" + inside
    )
    heredoc, _ = _forms(tmp_path, body)
    assert guard(heredoc, cwd=str(tmp_path))


@pytest.mark.parametrize("binding,nested", [
    ("match J():\n    case json: pass\n", False),
    ("match [J()]:\n    case [*json]: pass\n", False),
    ("match {'item': J()}:\n    case {**json}: pass\n", False),
    ("try: raise J()\nexcept J as json:\n", True),
    ("async def json(): pass\n", False),
])
def test_read_only_exemption_refuses_non_name_trusted_bindings(tmp_path, binding, nested):
    log = tmp_path / "intake.jsonl"
    log.write_text("hermes gateway " + "restart\n")
    loop = (
        f"for line in reversed(open('{log}').read().splitlines()):\n"
        "    d=json.loads(line)\n"
        "    print(d)\n"
    )
    ordinary, _ = _forms(tmp_path, "import json\n" + loop)
    unsafe = "import json, os\nclass J(Exception):\n    loads=staticmethod(os.system)\n" + binding
    unsafe += "".join("    " + line for line in loop.splitlines(keepends=True)) if nested else loop
    rebound, _ = _forms(tmp_path, unsafe)
    assert not guard(ordinary, cwd=str(tmp_path))
    assert guard(rebound, cwd=str(tmp_path))
