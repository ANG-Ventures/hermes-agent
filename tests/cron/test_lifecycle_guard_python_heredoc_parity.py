"""Python stdin and Python files have the same gateway-lifecycle verdict."""

import pytest

from cron.lifecycle_guard import (
    GatewayLifecycleBlocked,
    check_gateway_lifecycle,
    contains_gateway_lifecycle_command_or_referenced_script as guard,
)


def test_read_only_python_heredoc_does_not_execute_diagnostic_log(tmp_path):
    log = tmp_path / "intake.jsonl"
    log.write_text("hermes gateway " + "re" + "start\n")
    body = (
        "import subprocess\nfrom pathlib import Path\n"
        "subprocess.run(['sudo', '-n', 'sqlite3', "
        f"'file:{tmp_path / 'ledger.db'}?mode=ro', 'select count(*) from reviews'])\n"
        f"print(Path('{log}').read_text())\n"
    )
    script = tmp_path / "diagnose.py"
    script.write_text(body)
    assert not guard(f"python3 {script}", cwd=str(tmp_path))
    assert not guard(f"python3 - <<'PY'\n{body}PY", cwd=str(tmp_path))


@pytest.mark.parametrize("body", [
    "import subprocess\nsubprocess.run(['launchctl', 'bootout', 'system/ai.hermes.gateway'])\n",
    "import subprocess\nsubprocess.run(['kill', '$(pgrep -f hermes-gateway)'])\n",
    "import subprocess\nsubprocess.run(['systemctl', 'stop', 'hermes-gateway'])\n",
])
def test_python_heredoc_self_lifecycle_stays_blocked(tmp_path, body):
    script = tmp_path / "diagnose.py"
    script.write_text(body)
    assert guard(f"python3 - <<'PY'\n{body}PY", cwd=str(tmp_path))
    with pytest.raises(GatewayLifecycleBlocked):
        check_gateway_lifecycle("diagnose", script=str(script))


def test_python_heredoc_executing_referenced_script_stays_blocked(tmp_path):
    script = tmp_path / "restart.sh"
    script.write_text("hermes gateway " + "re" + "start\n")
    body = f"import os\nos.system('{script}')\n"
    assert guard(f"python3 - <<'PY'\n{body}PY", cwd=str(tmp_path))


def test_shadowed_path_name_cannot_hide_executed_script(tmp_path):
    script = tmp_path / "restart.sh"
    script.write_text("hermes gateway " + "re" + "start\n")
    body = (
        "from pathlib import Path\n"
        "Path = lambda value: __import__('os').system(value)\n"
        f"Path('{script}').read_text()\n"
    )
    assert guard(f"python3 - <<'PY'\n{body}PY", cwd=str(tmp_path))
