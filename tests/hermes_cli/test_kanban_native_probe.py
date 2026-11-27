"""Native SSH probe framing: Darwin has no /proc/loadavg."""
import json
import subprocess
from types import SimpleNamespace

import pytest
from hermes_cli import kanban_worker_pool as pool


def test_native_probe_executes_on_darwin_shape(tmp_path):
    pressure = tmp_path / "pressure with spaces.json"
    pressure.write_text(json.dumps({"at": 123, "cpu_busy": 0.2}))
    # Execute the actual remote command locally on Darwin, not invented stdout.
    host = pool.PoolHost(name="native", ssh_host="unused", slots=1, ssh_user="kanbanw", capacity_pct=0.9, absence="required", profiles=(), state="ready", enabled=True, priority=0)
    def runner(argv, **kwargs):
        return subprocess.run(argv[-1], shell=True, **kwargs)
    sample, error = pool._probe_once(host, runner, pressure_path=str(pressure))
    assert not error
    assert sample.ncpu < 1024  # timestamp must never slide into CPU count
    assert sample.remote_now > 123
    assert json.loads(sample.pressure_text)["at"] == 123


@pytest.mark.parametrize("field,value", [("ncpu", 0), ("load1", float("nan")), ("remote_now", None)])
def test_structured_probe_rejects_invalid_axes(field, value):
    doc = dict(schema=1, load1=1.2, load5=2.3, ncpu=18, remote_now=1234, pressure_text="{}")
    doc[field] = value
    host = pool.PoolHost(name="native", ssh_host="unused", slots=1, ssh_user="kanbanw", capacity_pct=0.9, absence="required", profiles=(), state="ready", enabled=True, priority=0)
    result, error = pool._probe_once(host, lambda *a, **k: SimpleNamespace(
        returncode=0, stdout="@@hermes-probe-v1\n" + json.dumps(doc) + "\n"), pressure_path="unused")
    assert result is None
    assert error


def test_missing_pressure_does_not_shift_native_header(tmp_path):
    host = pool.PoolHost(name="native", ssh_host="unused", slots=1, ssh_user="kanbanw", capacity_pct=0.9, absence="required", profiles=(), state="ready", enabled=True, priority=0)
    def runner(argv, **kwargs):
        return subprocess.run(argv[-1], shell=True, **kwargs)
    sample, error = pool._probe_once(host, runner, pressure_path=str(tmp_path / "absent"))
    assert not error
    assert sample.remote_now is not None
    assert sample.pressure_text == ""


def test_darwin_maps_only_default_linux_pressure_path(tmp_path):
    import sys
    if sys.platform != "darwin":
        pytest.skip("Darwin path projection")
    target = tmp_path / ".local/share/placement/host-pressure.json"
    target.parent.mkdir(parents=True)
    target.write_text('{"at": 123}')
    host = pool.PoolHost(name="native", ssh_host="unused", slots=None, ssh_user="kanbanw", capacity_pct=.9, absence="required", profiles=(), state="active", enabled=True, priority=0)
    def runner(argv, **kwargs):
        import os
        return subprocess.run(argv[-1], shell=True, env={**os.environ, "HOME": str(tmp_path)}, **kwargs)
    sample, error = pool._probe_once(host, runner, pressure_path="/var/lib/placement/host-pressure.json")
    assert not error
    assert json.loads(sample.pressure_text)["at"] == 123


def test_corrupt_pressure_preserves_native_liveness_and_workspace(tmp_path):
    pressure = tmp_path / "corrupt.json"
    pressure.write_bytes(bytes([255]))
    workspace = tmp_path / "default" / "task"
    workspace.mkdir(parents=True)
    host = pool.PoolHost("native", "unused", "kanbanw", None, .9, "required", (), "active", True, 0)
    def runner(argv, **kwargs):
        return subprocess.run(argv[-1], shell=True, **kwargs)
    sample, error = pool._probe_once(host, runner, pressure_path=str(pressure),
                                    workspaces_root=str(tmp_path), workspace_paths=[str(workspace)])
    assert not error
    assert sample.ncpu > 0
    assert sample.workspace_states[str(workspace)] == "empty"
    assert sample.pressure_text in ("", "\ufffd")


def test_native_cpu_measurement_ignores_python_cpu_override(tmp_path):
    import os
    host = pool.PoolHost("native", "unused", "kanbanw", None, .9, "required", (), "active", True, 0)
    def runner(argv, **kwargs):
        return subprocess.run(argv[-1], shell=True, env={**os.environ, "PYTHON_CPU_COUNT": "123456"}, **kwargs)
    sample, error = pool._probe_once(host, runner, pressure_path=str(tmp_path / "absent"))
    assert not error
    assert sample.ncpu == os.sysconf("SC_NPROCESSORS_ONLN")
