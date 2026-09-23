"""The Windows hand-off keeps serving progress while its main thread blocks.

windows.ps1 answers /progress from a dedicated runspace precisely so the
window keeps moving through the long silent stretches (`hermes update`, pip,
the desktop rebuild) that made an 18-minute update look hung. This drives the
real script and polls the real listener; the posix half of the same contract
is covered in test_desktop_update_shim_progress.py.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from urllib.request import urlopen

import pytest

pytestmark = pytest.mark.windows_only

REPO_ROOT = Path(__file__).resolve().parent.parent
WINDOWS_UPDATE_PS1 = REPO_ROOT / "scripts" / "desktop-update" / "windows.ps1"


@pytest.fixture(scope="module", autouse=True)
def _loaded_runner():
    """Temporary hosted-Windows one-core contention proof; not for the final PR."""
    worker = subprocess.Popen(
        [sys.executable, "-c", "import time\nend = time.monotonic() + 900\nwhile time.monotonic() < end: pass"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        yield
    finally:
        worker.terminate()
        worker.wait(timeout=10)


def _read_progress(url: str, deadline: float) -> dict[str, object]:
    """Poll /progress, retrying transient socket stalls until ``deadline``.

    A single slow answer from the PS runspace listener is NOT the bug this
    test guards (the listener can lose the CPU for seconds on a loaded CI
    runner while it still serves fine a moment later). One raw
    ``urlopen(timeout=5)`` propagating TimeoutError was exactly the Aug 2026
    flake (run 32440286339). Only a listener that stays unresponsive until
    the deadline fails the test.
    """
    last_exc: Exception | None = None
    attempted = False
    while not attempted or time.monotonic() < deadline:
        attempted = True
        try:
            with urlopen(f"{url}progress", timeout=5) as response:
                return json.loads(response.read().decode("utf-8"))
        except (TimeoutError, OSError) as exc:  # transient stall — retry
            last_exc = exc
            time.sleep(0.2)
    raise AssertionError(
        f"/progress unresponsive until deadline (last error: {last_exc!r})"
    )


@pytest.mark.parametrize("trial", range(20))
def test_progress_advances_while_the_orchestrator_blocks(tmp_path: Path, trial: int) -> None:
    powershell = shutil.which("powershell.exe")
    assert powershell, "Windows updater tests require Windows PowerShell."

    output_path = tmp_path / "self-test-output.log"
    env = os.environ.copy()
    env["TEMP"] = str(tmp_path)
    env["TMP"] = str(tmp_path)
    release_path = tmp_path / "release-self-test"
    env["HERMES_SELFTEST_RELEASE_FILE"] = str(release_path)
    # If the release-file branch is ignored, the fallback timer must expire
    # before we can sample even one progress tick.
    env["HERMES_SELFTEST_HOLD_SECONDS"] = "0"

    with output_path.open("wb") as output:
        process = subprocess.Popen(
            [
                powershell,
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(WINDOWS_UPDATE_PS1),
                "-SelfTestUi",
                "-NoUi",
            ],
            stdout=output,
            stderr=subprocess.STDOUT,
            env=env,
        )

    try:
        # Wait for the script's readiness event, not for PowerShell cold start
        # to fit inside a 20s scheduling window. The CI failure had no output
        # at all: it never reached the shim, let alone the progress assertion.
        # This deadline is only a backstop for a genuinely stuck child.
        deadline = time.monotonic() + 120
        shim_url = None
        while time.monotonic() < deadline:
            text = output_path.read_text(encoding="utf-8", errors="replace")
            match = re.search(r"SELF-TEST: shim at (http://127\.0\.0\.1:\d+/)", text)
            if match:
                shim_url = match.group(1)
                break
            if process.poll() is not None:
                break
            time.sleep(0.1)

        assert shim_url, output_path.read_text(encoding="utf-8", errors="replace")
        assert process.poll() is None, "self-test exited before release file was written"

        # The URL prints BEFORE the orchestrator publishes its held stage —
        # sampling immediately races the publish and can catch the page's
        # boot default instead ('Hermes will open once done.' ==
        # 'Testing quiet update', PR #90358 first run). Wait for the held
        # stage to actually land, THEN start the stability window.
        held_stage = "Testing quiet update"
        publish_deadline = time.monotonic() + 10
        try:
            first = _read_progress(shim_url, publish_deadline)
        except AssertionError as exc:
            raise AssertionError("self-test listener closed before release file was written") from exc
        while first.get("message") != held_stage and time.monotonic() < publish_deadline:
            time.sleep(0.1)
            first = _read_progress(shim_url, publish_deadline)
        assert first["message"] == held_stage, first

        # Observe an actual listener clock tick rather than sampling after a
        # fixed sleep. The orchestrator stays held until we release it, even
        # if either the test or listener loses the CPU between samples.
        progress_deadline = time.monotonic() + 20
        second = _read_progress(shim_url, progress_deadline)
        while int(second["elapsed_seconds"]) <= int(first["elapsed_seconds"]) and time.monotonic() < progress_deadline:
            time.sleep(0.1)
            second = _read_progress(shim_url, progress_deadline)

        # The stage is whatever the orchestrator last published -- it must
        # reach the page verbatim and must not churn on its own.
        assert first["status"] == "running"
        assert first["message"]
        assert second["message"] == first["message"]
        # The main thread is asleep for the whole window above. If elapsed
        # only moved when the orchestrator published, it would be frozen here
        # -- which is what a stalled update looks like to the user.
        assert int(second["elapsed_seconds"]) > int(first["elapsed_seconds"])

        release_path.touch()
        assert process.wait(timeout=60) == 0
    finally:
        release_path.touch()
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
