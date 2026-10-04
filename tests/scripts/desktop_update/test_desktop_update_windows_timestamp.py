"""The Windows handoff writes numeric Unix seconds under comma-decimal locales."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import time

import pytest


@pytest.mark.platforms("windows")
def test_windows_update_writes_locale_independent_marker_and_result(tmp_path, monkeypatch):
    shell = shutil.which("powershell.exe")
    assert shell, "native Windows acceptance requires PowerShell"
    script = Path(__file__).resolve().parent.parent.parent.parent / "scripts/desktop-update/windows.ps1"
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delenv("HERMES_UPDATE_STARTED_AT", raising=False)
    # -SelfTestMarker runs the actual claim and finally/result publication,
    # but never updates a checkout, waits on Desktop, or launches processes.
    command = (
        "[Threading.Thread]::CurrentThread.CurrentCulture = [Globalization.CultureInfo]::GetCultureInfo('es-ES'); "
        "& $env:HERMES_TIMESTAMP_TEST_SCRIPT -InstallRoot $env:HERMES_HOME -NoUi -NoMarkerCleanup -SelfTestMarker"
    )
    started = int(time.time())
    # 300 s, not upstream's 60: the fork's lane is hosted windows-latest (4 vCPU, 8 files
    # in flight), not windows-latest-32-core, and the self-test outran 60 s there
    # (run 37081194693, TimeoutExpired). The marker/receipt bounds below are the contract;
    # the deadline only has to outlast a loaded runner.
    result = subprocess.run([shell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command],
                            env={**os.environ, "HERMES_TIMESTAMP_TEST_SCRIPT": str(script)},
                            capture_output=True, text=True, timeout=300)
    finished = int(time.time())
    assert result.returncode == 0, result.stdout + result.stderr
    marker = (tmp_path / ".hermes-update-in-progress").read_text(encoding="utf-8-sig").splitlines()
    receipt = json.loads((tmp_path / ".hermes-update-result.json").read_text(encoding="utf-8-sig"))
    assert len(marker) == 2 and int(marker[0]) > 0
    assert started <= int(marker[1]) <= finished
    assert type(receipt["finished_at"]) is int
    assert started <= receipt["finished_at"] <= finished
    assert receipt["exit_code"] == 0