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
