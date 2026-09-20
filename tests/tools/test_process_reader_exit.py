"""Output EOF is not evidence that a background command has exited."""
import shlex
import subprocess
import sys
import threading

import pytest

from tools.process_registry import ProcessRegistry, ProcessSession


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX shell redirection")
def test_redirected_command_does_not_complete_at_stdout_eof(tmp_path, monkeypatch):
    registry = ProcessRegistry()
    monkeypatch.setattr(registry, "_write_checkpoint", lambda: None)
    # Explicit exec makes shell last-command optimization deterministic.
    # Redirection closes the capture pipe before the command exits.
    release = tmp_path / "release"
    script = (
        "import pathlib,time; "
        f"p=pathlib.Path({str(release)!r}); "
        "exec('while not p.exists(): time.sleep(0.01)')"
    )
    command = f"cd {shlex.quote(str(tmp_path))} && env FOO=1 {shlex.quote(sys.executable)} -c {shlex.quote(script)} > out.log 2>&1"
    proc = subprocess.Popen(
        ["/bin/sh", "-c", command.replace("&& env", "&& exec env")],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, cwd=tmp_path,
    )
    session = ProcessSession(id="proc_eof", command=command, process=proc,
                             pid=proc.pid, notify_on_complete=True)
    registry._running[session.id] = session
    waiting = threading.Event()
    real_wait = proc.wait

    def wait(timeout=None):
        waiting.set()
        return real_wait(timeout=timeout)

    monkeypatch.setattr(proc, "wait", wait)
    thread = threading.Thread(target=registry._reader_loop, args=(session,), daemon=True)
    thread.start()
    try:
        assert waiting.wait(5), "reader never reached EOF/wait"
        # Old code abandons wait after five seconds and enqueues a null exit.
        assert not session._completion_event.wait(5.5)
        assert proc.poll() is None
        assert session.stdout_closed
        assert registry.completion_queue.empty()
        release.touch()
        thread.join(5)
        assert not thread.is_alive()
        event = registry.completion_queue.get_nowait()
        assert event["exit_code"] == 0
        assert event["completion_reason"] == "exited"
        assert registry.completion_queue.empty()
    finally:
        release.touch()
        proc.wait(timeout=5)
        thread.join(5)
        proc.stdout.close()


def test_missing_process_handle_is_not_an_exit(monkeypatch):
    registry = ProcessRegistry()
    monkeypatch.setattr(registry, "_write_checkpoint", lambda: None)
    session = ProcessSession(id="proc_missing", command="lost", notify_on_complete=True)
    registry._running[session.id] = session
    registry._reader_loop(session)
    event = registry.completion_queue.get_nowait()
    assert event["completion_reason"] == "handle-lost"
    assert event["exit_code"] is None
    assert registry.completion_queue.empty()


def test_registry_rejects_exited_with_unknown_status(monkeypatch, caplog):
    registry = ProcessRegistry()
    monkeypatch.setattr(registry, "_write_checkpoint", lambda: None)
    session = ProcessSession(id="proc_unknown", command="unknown", exited=True,
                             notify_on_complete=True)
    registry._running[session.id] = session
    registry._move_to_finished(session)
    registry._move_to_finished(session)
    event = registry.completion_queue.get_nowait()
    assert event["completion_reason"] == "handle-lost"
    assert event["exit_code"] is None
    assert "Refusing unknown exit status" in caplog.text
    assert registry.completion_queue.empty()


@pytest.mark.parametrize("signalstatus, expected", [(15, -15), (None, None)])
def test_pty_without_exitstatus_is_not_a_normal_exit(monkeypatch, signalstatus, expected):
    from types import SimpleNamespace

    registry = ProcessRegistry()
    monkeypatch.setattr(registry, "_write_checkpoint", lambda: None)
    session = ProcessSession(id="proc_pty", command="pty", notify_on_complete=True)
    session._pty = SimpleNamespace(
        isalive=lambda: False, wait=lambda: None,
        exitstatus=None, signalstatus=signalstatus,
    )
    registry._running[session.id] = session
    registry._pty_reader_loop(session)
    event = registry.completion_queue.get_nowait()
    assert event["exit_code"] == expected
    assert event["completion_reason"] == ("exited" if expected is not None else "handle-lost")
