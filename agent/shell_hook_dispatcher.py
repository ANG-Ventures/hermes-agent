"""Run several Python shell hooks from ONE interpreter start (driver: agent/shell_hooks_batch.py, t_f010356f).

Hermes starts ``<python> shell_hook_dispatcher.py`` once per tool call instead of one
``<python> <hook>.py`` per configured hook. stdin is one JSON object
``{"hooks": [{"script", "args", "timeout"}, ...], "stdin": "<hook payload>"}``.

Each hook runs in a forked child that looks to the hook like a fresh ``<python> <script> <args...>``:
its own process group, the payload on a pipe at fd 0, its own stdout / stderr pipes, ``sys.argv``,
``sys.path[0]`` (the script's real directory), a fresh ``__main__``, ``os.getppid()`` naming the
process that started the dispatcher (the agent, as before), and the exit status the interpreter
would report (``SystemExit`` mapping, traceback + 1 on an uncaught exception, non-daemon threads
and atexit handlers finished first). Every child forks from the same pristine dispatcher, so no
hook sees another hook's imports or state. All hooks start at once. A hook is finished when it has
exited and closed both pipes (what ``Popen.communicate`` waits for); at its timeout its whole
process group is killed.

stdout: one JSON line per finished hook, in completion order:
``{"i", "rc", "out", "err", "timed_out", "elapsed"}`` (``out``/``err`` base64 of the raw bytes),
or ``{"i", "missing": true}`` when the script could not be opened; the driver then runs that hook
the old way, so the interpreter's own error and the missing-hook self-heal stay unchanged.

Stdlib only, Python 3.9-compatible: it runs under whatever interpreter the hooks name.
"""

import base64
import json
import os
import selectors
import signal
import sys
import time
import traceback
import types

# Loaded once here so each forked hook finds them already imported (policy hooks import them).
import re  # noqa: F401
import shlex  # noqa: F401
import subprocess  # noqa: F401
from importlib.machinery import SourceFileLoader

_AGENT_PID = os.getppid()
_CHUNK = 65536


def _exit_code(exc):
    """SystemExit -> status, as the interpreter maps it."""
    code = exc.code
    if code is None:
        return 0
    if isinstance(code, int):
        return code & 0xFF
    try:
        sys.stderr.write(str(code) + "\n")
    except Exception:
        pass
    return 1


def _strip_own_frames(tb):
    while tb is not None and tb.tb_frame.f_code.co_filename == _SELF_CODE_FILE:
        tb = tb.tb_next
    return tb


def _finish_interpreter():
    """What interpreter shutdown does before exit: join non-daemon threads, run atexit."""
    threading = sys.modules.get("threading")
    if threading is not None:
        try:
            threading._shutdown()
        except Exception:
            pass
    try:
        import atexit
        atexit._run_exitfuncs()
    except Exception:
        pass


def _close_inherited_fds():
    """Close every fd above 2 (the other hooks' pipes), as a fresh Popen child has none of them."""
    try:
        fds = [int(name) for name in os.listdir("/dev/fd")]
    except (OSError, ValueError):
        os.closerange(3, 65536)
        return
    for fd in fds:
        if fd > 2:
            try:
                os.close(fd)
            except OSError:
                pass


def _run_child(script, args, in_fd, out_fd, err_fd):
    """In the forked child: become ``python <script> <args>``; never returns."""
    status = 1
    try:
        os.setpgid(0, 0)
        os.dup2(in_fd, 0)
        os.dup2(out_fd, 1)
        os.dup2(err_fd, 2)
        _close_inherited_fds()
        signal.signal(signal.SIGINT, signal.default_int_handler)
        signal.signal(signal.SIGCHLD, signal.SIG_DFL)  # windows-footgun: ok — POSIX fork child; on Windows os.fork fails first and shell_hooks_batch runs hooks one by one
        orig_in, orig_out, orig_err = sys.__stdin__, sys.__stdout__, sys.__stderr__
        sys.stdin = open(0, "r", encoding=orig_in.encoding, errors=orig_in.errors, closefd=False)
        sys.stdout = open(1, "w", encoding=orig_out.encoding, errors=orig_out.errors, closefd=False)
        sys.stderr = open(2, "w", encoding=orig_err.encoding, errors=orig_err.errors, closefd=False)
        sys.__stdin__, sys.__stdout__, sys.__stderr__ = sys.stdin, sys.stdout, sys.stderr
        os.getppid = lambda: _AGENT_PID
        path = os.path.abspath(script)
        sys.argv = [script] + list(args)
        sys.path[0] = os.path.dirname(os.path.realpath(script))
        main = types.ModuleType("__main__")
        main.__file__ = path
        main.__builtins__ = __builtins__
        main.__loader__ = SourceFileLoader("__main__", path)
        _keep.append(sys.modules.get("__main__"))  # the dispatcher's own module stays alive
        sys.modules["__main__"] = main
        try:
            with open(script, "rb") as fh:
                source = fh.read()
            exec(compile(source, path, "exec"), main.__dict__)
            status = 0
        except SystemExit as exc:
            status = _exit_code(exc)
        except BaseException:
            exc_type, exc, tb = sys.exc_info()
            tb = _strip_own_frames(tb)
            exc.__traceback__ = tb  # 3.13+ excepthook prints exc.__traceback__, not the tb argument
            sys.excepthook(exc_type, exc, tb)
            status = 1
        _finish_interpreter()
        for stream in (sys.stdout, sys.stderr):
            try:
                stream.flush()
            except Exception:
                status = status or 120
    except BaseException:
        try:
            traceback.print_exc()
        except Exception:
            pass
    finally:
        os._exit(status)


def _returncode(status):
    if os.WIFEXITED(status):
        return os.WEXITSTATUS(status)
    if os.WIFSIGNALED(status):
        return -os.WTERMSIG(status)
    return status


def _emit(row):
    sys.stdout.write(json.dumps(row) + "\n")
    sys.stdout.flush()


class _Hook:
    def __init__(self, i, pid, in_w, out_r, err_r, timeout, payload):
        self.i, self.pid = i, pid
        self.t0 = time.monotonic()
        self.deadline = self.t0 + float(timeout or 60)
        self.payload = memoryview(payload)
        self.in_w = in_w
        self.open = {out_r: bytearray(), err_r: bytearray()}
        self.out_r, self.err_r = out_r, err_r
        self.status = None
        self.out = self.err = b""


def _start(i, hook, payload):
    script, args = hook["script"], hook.get("args") or []
    try:
        with open(script, "rb"):
            pass
    except OSError:
        return None
    in_r, in_w = os.pipe()
    out_r, out_w = os.pipe()
    err_r, err_w = os.pipe()
    sys.stdout.flush()
    sys.stderr.flush()
    pid = os.fork()  # windows-footgun: ok — POSIX fork model; on Windows this raises and shell_hooks_batch runs hooks one by one
    if pid == 0:
        _run_child(script, args, in_r, out_w, err_w)
    for fd in (in_r, out_w, err_w):
        os.close(fd)
    for fd in (in_w, out_r, err_r):
        os.set_blocking(fd, False)
    return _Hook(i, pid, in_w, out_r, err_r, hook.get("timeout"), payload)


def _close(sel, fd):
    try:
        sel.unregister(fd)
    except (KeyError, ValueError):
        pass
    try:
        os.close(fd)
    except OSError:
        pass


def _finish(sel, h, timed_out):
    if h.in_w is not None:
        _close(sel, h.in_w)
        h.in_w = None
    for fd in list(h.open):
        _close(sel, fd)
    if timed_out:
        _emit({"i": h.i, "rc": None, "out": "", "err": "", "timed_out": True,
               "elapsed": round(time.monotonic() - h.t0, 3)})
    else:
        _emit({"i": h.i, "rc": _returncode(h.status), "timed_out": False,
               "out": base64.b64encode(bytes(h.out)).decode("ascii"),
               "err": base64.b64encode(bytes(h.err)).decode("ascii"),
               "elapsed": round(time.monotonic() - h.t0, 3)})


def main():
    request = json.loads(sys.stdin.read())
    payload = request["stdin"].encode("utf-8")
    sel = selectors.DefaultSelector()
    live = []
    for i, hook in enumerate(request["hooks"]):
        h = _start(i, hook, payload)
        if h is None:
            _emit({"i": i, "missing": True})
            continue
        live.append(h)
    for h in live:
        if len(h.payload):
            sel.register(h.in_w, selectors.EVENT_WRITE, (h, "in"))
        else:
            os.close(h.in_w)
            h.in_w = None
        sel.register(h.out_r, selectors.EVENT_READ, (h, "out"))
        sel.register(h.err_r, selectors.EVENT_READ, (h, "err"))
    while live:
        now = time.monotonic()
        wait = max(0.0, min(h.deadline for h in live) - now)
        for key, _ in sel.select(timeout=min(wait, 0.05)):
            h, kind = key.data
            if kind == "in":
                try:
                    n = os.write(h.in_w, h.payload[:_CHUNK])
                    h.payload = h.payload[n:]
                except (BrokenPipeError, OSError):
                    h.payload = h.payload[:0]
                if not len(h.payload):
                    _close(sel, h.in_w)
                    h.in_w = None
                continue
            try:
                data = os.read(key.fd, _CHUNK)
            except BlockingIOError:
                continue
            except OSError:
                data = b""
            if data:
                h.open[key.fd] += data
            else:
                buf = h.open.pop(key.fd)
                if key.fd == h.out_r:
                    h.out = buf
                else:
                    h.err = buf
                _close(sel, key.fd)
        now = time.monotonic()
        for h in list(live):
            if h.status is None:
                pid, status = os.waitpid(h.pid, os.WNOHANG)
                if pid:
                    h.status = status
            if h.status is not None and not h.open:
                _finish(sel, h, False)
                live.remove(h)
            elif now >= h.deadline:
                try:
                    os.killpg(h.pid, signal.SIGKILL)  # windows-footgun: ok — POSIX fork model; never reached on Windows (os.fork fails first)
                except OSError:
                    pass
                if h.status is None:
                    os.waitpid(h.pid, 0)
                _finish(sel, h, True)
                live.remove(h)


_keep = []
_SELF_CODE_FILE = _run_child.__code__.co_filename

if __name__ == "__main__":
    main()
