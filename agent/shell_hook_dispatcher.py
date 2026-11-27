"""Run several Python shell hooks from ONE interpreter start (driver: agent/shell_hooks_batch.py, t_f010356f).

Hermes keeps ``<python> shell_hook_dispatcher.py --serve`` warm and sends it one request per tool
call, instead of one ``<python> <hook>.py`` per configured hook. A request is one JSON object
``{"hooks": [{"script", "args", "timeout"}, ...], "stdin": "<hook payload>"}``: one per stdin line
with ``--serve`` (each answered by its rows then ``{"end": true}``), or all of stdin without it.

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
plus ``"skipped": true`` for a hook its prefilter skipped (below),
or ``{"i", "missing": true}`` when the script could not be opened; the driver then runs that hook
the old way, so the interpreter's own error and the missing-hook self-heal stay unchanged.

Compile once (t_492356b3): the dispatcher compiles each hook's source in the warm parent, keyed on
(path, device, inode, mtime_ns, size), and the forked child execs that code object instead of
re-reading and re-compiling the file. Same ``compile(source, abspath, "exec")`` call, so the hook
sees the same code, ``__file__``, tracebacks and argv. A file that does not compile is not cached;
its child compiles it the old way and reports the error exactly as before. An edit on disk changes
the key and is recompiled on the next request.

Prefilter (t_492356b3, opt-in): a hook may declare, as ONE top-level literal assignment
``HOOK_PREFILTER = {"tools": [...], "tokens": [...], "tool_input_tokens": [...]}`` (any of the
keys, non-empty string lists), which calls it can act on. The dispatcher reads it with ``ast``
(never executes it) once per
compile. A hook is not forked, and answers ``rc 0`` with stdout ``{}`` (the no-op every hook mode
accepts) plus ``"skipped": true``, only when its declaration provably misses: ``tools`` names the
tool calls it acts on and the payload's ``tool_name`` is a string outside it; ``tokens`` are
substrings of which none occurs in the raw payload, in any decoded string in it (keys and values),
or in that string re-joined after ``shlex.split`` (so shell quoting such as ``g''it`` still matches
``git``); when any string in the payload does not shell-split, tokens never skip. Because the raw
text always carries the envelope's ``{``, ``}`` and ``,``, a ``tokens`` list containing one of those
can never skip; ``tool_input_tokens`` (t_7d9b8ad4) is the same test scoped to the decoded strings
(keys and values, recursively) under ``tool_input`` and their shlex re-joins only, never the raw
JSON, so ``{`` matches only a ``{`` the tool call itself carries. It never skips when ``tool_input``
is absent or not an object, or when a string under it does not shell-split. ``tokens`` keeps its
raw-text scope for existing declarations. A hook without a
valid declaration, or a payload that is not a JSON object, runs as before. A hook file written
while the dispatcher reads it is not cached. Tokens cannot
see through shell indirection (``$X``, ``eval``): declare tokens only when the hook itself acts on
nothing but the literal text.

Stdlib only, Python 3.9-compatible: it runs under whatever interpreter the hooks name.
"""

import ast
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
_SKIPPED = object()
_NOOP_OUT = base64.b64encode(b"{}").decode("ascii")
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


_PREFILTER_NAME = "HOOK_PREFILTER"
# abspath -> (stat key, code or None, prefilter or None). One entry per hook path.
_compiled = {}


def _stat_key(st):
    return (st.st_dev, st.st_ino, st.st_mtime_ns, st.st_size)


def _read_prefilter(source):
    """The hook's ``HOOK_PREFILTER`` declaration, validated, or None (run the hook as before)."""
    if _PREFILTER_NAME.encode("ascii") not in source:
        return None
    try:
        tree = ast.parse(source)
    except Exception:
        return None
    values = []
    for node in tree.body:
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets = [node.target]
        else:
            continue
        if any(isinstance(t, ast.Name) and t.id == _PREFILTER_NAME for t in targets):
            values.append(node.value)
    if len(values) != 1:
        return None
    try:
        decl = ast.literal_eval(values[0])
    except Exception:
        return None
    if not isinstance(decl, dict) or not decl or set(decl) - {"tools", "tokens", "tool_input_tokens"}:
        return None
    out = {}
    for key, items in decl.items():
        if not isinstance(items, (list, tuple)) or not items:
            return None
        if not all(isinstance(s, str) and s for s in items):
            return None
        out[key] = frozenset(items) if key == "tools" else tuple(items)
    return out


def _load(script):
    """``(code, prefilter)`` for the script, compiled once per (path, dev, inode, mtime, size).
    ``code`` None: the child reads and compiles the file itself, exactly as before."""
    path = os.path.abspath(script)
    try:
        key = _stat_key(os.stat(path))
    except OSError:
        return None, None
    hit = _compiled.get(path)
    if hit is not None and hit[0] == key:
        return hit[1], hit[2]
    try:
        with open(path, "rb") as fh:
            before = _stat_key(os.fstat(fh.fileno()))
            source = fh.read()
            after = _stat_key(os.fstat(fh.fileno()))
        code = compile(source, path, "exec")
    except Exception:
        _compiled.pop(path, None)
        return None, None
    prefilter = _read_prefilter(source)
    if before != after or after != key:
        # Written while we read: the bytes match no single version. Use nothing cached; the child
        # reads the file itself, and the next request compiles the settled version.
        _compiled.pop(path, None)
        return None, None
    _compiled[path] = (key, code, prefilter)
    return code, prefilter


def _strings(value, out):
    if isinstance(value, str):
        out.append(value)
    elif isinstance(value, dict):
        for k, v in value.items():
            _strings(k, out)
            _strings(v, out)
    elif isinstance(value, list):
        for v in value:
            _strings(v, out)


def _haystack(found, head=()):
    """Each string plus its ``shlex.split`` re-join, newline-joined; None when one does not split."""
    parts = list(head)
    for s in found:
        parts.append(s)
        try:
            parts.append(" ".join(shlex.split(s)))
        except ValueError:
            return None
    return "\n".join(parts)


def _payload_view(payload):
    """``(tool_name, haystack, input_haystack)`` for prefilter matching, or None when the payload is
    not a JSON object. ``haystack`` (for ``tokens``) is the raw text plus every decoded string;
    ``input_haystack`` (for ``tool_input_tokens``) is only the decoded strings under ``tool_input``,
    and None when ``tool_input`` is absent or not an object. Either is None when one of its strings
    does not shell-split: then no token miss is provable (a hook may parse it another way)."""
    try:
        text = payload.decode("utf-8")
        data = json.loads(text)
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    found = []
    _strings(data, found)
    tool_input = data.get("tool_input")
    input_haystack = None
    if isinstance(tool_input, dict):
        scoped = []
        _strings(tool_input, scoped)
        input_haystack = _haystack(scoped)
    return data.get("tool_name"), _haystack(found, (text,)), input_haystack


def _prefilter_skips(prefilter, view):
    """True only when the hook's declaration provably does not match this call."""
    if prefilter is None or view is None:
        return False
    tool_name, haystack, input_haystack = view
    tools = prefilter.get("tools")
    if tools is not None and isinstance(tool_name, str) and tool_name not in tools:
        return True
    for key, hay in (("tokens", haystack), ("tool_input_tokens", input_haystack)):
        tokens = prefilter.get(key)
        if tokens is not None and hay is not None and not any(t in hay for t in tokens):
            return True
    return False


def _run_child(script, args, in_fd, out_fd, err_fd, code=None):
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
            if code is None:
                with open(script, "rb") as fh:
                    source = fh.read()
                code = compile(source, path, "exec")
            exec(code, main.__dict__)
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


def _start(i, hook, payload, view):
    """A running ``_Hook``; None when the script cannot be opened; ``_SKIPPED`` when its prefilter
    provably misses (``view()`` gives the payload's prefilter view, built once per request)."""
    script, args = hook["script"], hook.get("args") or []
    try:
        with open(script, "rb"):
            pass
    except OSError:
        return None
    code, prefilter = _load(script)
    if prefilter is not None and _prefilter_skips(prefilter, view()):
        return _SKIPPED
    in_r, in_w = os.pipe()
    out_r, out_w = os.pipe()
    err_r, err_w = os.pipe()
    sys.stdout.flush()
    sys.stderr.flush()
    pid = os.fork()  # windows-footgun: ok — POSIX fork model; on Windows this raises and shell_hooks_batch runs hooks one by one
    if pid == 0:
        _run_child(script, args, in_r, out_w, err_w, code)
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
    """One request on stdin (read to EOF), or with ``--serve`` one request per stdin line until EOF,
    each answered by its rows and then ``{"end": true}`` (the warm dispatcher, t_e920d269)."""
    if "--serve" in sys.argv[1:]:
        while True:
            line = sys.stdin.buffer.readline()
            if not line:
                return
            _run_request(json.loads(line))
            _emit({"end": True})
    _run_request(json.loads(sys.stdin.read()))


def _run_request(request):
    payload = request["stdin"].encode("utf-8")
    sel = selectors.DefaultSelector()
    try:
        _run_hooks(request, payload, sel)
    finally:
        sel.close()


def _run_hooks(request, payload, sel):
    live = []
    views = []

    def view():
        if not views:
            views.append(_payload_view(payload))
        return views[0]

    for i, hook in enumerate(request["hooks"]):
        h = _start(i, hook, payload, view)
        if h is None:
            _emit({"i": i, "missing": True})
            continue
        if h is _SKIPPED:
            _emit({"i": i, "rc": 0, "out": _NOOP_OUT, "err": "", "timed_out": False, "elapsed": 0.0,
                   "skipped": True})
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
                except (BlockingIOError, InterruptedError):
                    # A nonblocking pipe can become full after readiness was reported.
                    # Keep the unwritten suffix and retry; closing here truncates hook JSON.
                    continue
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
