"""Kanban worker CPU priority: default NORMAL since 2026-10-19; background/idle opt-in.

2026-10-19 (Ace 21:52, card t_6ffdd0c4): the dev pipeline, kanban workers included, is
first-class: the default spawn carries no ``taskpolicy`` prefix and no nice. The history
below is why ``background``/``idle`` still exist as per-host opt-ins.

Originally (2026-09-20 incident), worker gateways ran at BACKGROUND CPU priority.

A daedalus-opus kanban worker launched 384 busy-loops in the background on the
production Mac Studio and never reaped them. Load average reached 538 for 55
minutes. The RESIDENT gateway on the same host starved: its loop-liveness
watchdog fired twice ("missed 3 consecutive liveness probes; exiting with code
75"), a subsequent boot took 12 minutes, and all four platform adapters timed
out at boot — including the localhost webhook listener. The agent was deaf on
every surface until the burners were killed by hand.

The class fix is priority, not policing: a kanban worker is batch work and must
never be able to outbid the interactive gateway for CPU. ``_default_spawn``
applies ``nice 19`` (and SCHED_IDLE where available) in a ``preexec_fn``, so
every descendant the worker spawns — its terminal-tool children included —
inherits the deprioritisation. Since 2026-10-19 that is opt-in
(``kanban.worker_cpu_priority: background``).

The detector half is in ``tests/gateway/test_loop_liveness_load_context.py``.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

# Ensure the worktree (not a stale global clone) is first on sys.path.
_WORKTREE = Path(__file__).resolve().parents[2]
if str(_WORKTREE) not in sys.path:
    sys.path.insert(0, str(_WORKTREE))

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_dispatch as kbd


@pytest.fixture
def fresh_home(tmp_path, monkeypatch):
    home = tmp_path / "hermes_home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    for var in (
        "HERMES_KANBAN_DB",
        "HERMES_KANBAN_WORKSPACES_ROOT",
        "HERMES_KANBAN_HOME",
        "HERMES_KANBAN_BOARD",
    ):
        monkeypatch.delenv(var, raising=False)
    try:
        import hermes_constants

        hermes_constants._cached_default_hermes_root = None  # type: ignore[attr-defined]
    except Exception:
        pass
    kb._INITIALIZED_PATHS.clear()
    return home


def _task(task_id: str = "t_prio") -> "kb.Task":
    return kb.Task(
        id=task_id,
        title="cpu priority",
        body=None,
        assignee="teknium",
        status="ready",
        priority=0,
        created_by=None,
        created_at=0,
        started_at=None,
        completed_at=None,
        workspace_kind="scratch",
        workspace_path=None,
        claim_lock=None,
        claim_expires=None,
        tenant=None,
    )


def _spawn_and_capture_kwargs(fresh_home, monkeypatch, task_id="t_prio") -> dict:
    """Drive ``_default_spawn`` with ``Popen`` stubbed; return its kwargs."""
    captured: dict = {}

    class FakeProc:
        pid = 4242

    def fake_popen(cmd, *args, **kwargs):
        captured.update(kwargs)
        captured["cmd"] = cmd
        return FakeProc()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    kbd._default_spawn(_task(task_id), str(fresh_home / "ws"), board=None)
    return captured


# ---------------------------------------------------------------------------
# (a) the spawn helper applies nice 19 — asserted in a REAL child process
# ---------------------------------------------------------------------------


def _launch_with_captured_prefix(captured: dict, program: str, **kw):
    """Run ``program`` under the launcher prefix _default_spawn put on the worker argv.

    The prefix is everything before the worker's own argv (``--`` ends it), so
    the probe child goes through exactly the exec-form launcher the worker does.
    """
    cmd = captured["cmd"]
    assert "--" in cmd, f"_default_spawn put no limits launcher on the argv: {cmd[:6]}"
    prefix = cmd[: cmd.index("--") + 1]
    return subprocess.run([*prefix, sys.executable, "-c", program], capture_output=True, text=True, **kw)


@pytest.mark.skipif(
    not hasattr(os, "setpriority"), reason="POSIX priority API unavailable"
)
def test_worker_launcher_applies_nice_19_in_a_real_child(fresh_home, monkeypatch):
    """The launcher _default_spawn prefixes really deprioritises a child.

    This runs the prefix against a trivial ``python -c`` child (never a gateway)
    and reads the priority back from inside that child via ``os.getpriority``.
    A launcher that is wired but inert would return 0 here. No preexec_fn
    (t_b9a61253): a preexec_fn forces fork_exec, a whole-gateway fork on macOS.
    """
    monkeypatch.setattr(kb, "worker_cpu_priority_config", lambda cfg=None: ("background", 19))
    captured = _spawn_and_capture_kwargs(fresh_home, monkeypatch)
    assert captured.get("preexec_fn") is None

    monkeypatch.undo()  # real Popen for the live child probe
    out = _launch_with_captured_prefix(
        captured, "import os; print(os.getpriority(os.PRIO_PROCESS, 0))", timeout=60)
    assert out.returncode == 0, out.stderr
    assert int(out.stdout.strip()) == 19, (
        f"child niceness was {out.stdout.strip()!r}, expected 19"
    )


@pytest.mark.skipif(
    not hasattr(os, "setpriority") or not hasattr(os, "fork"),
    reason="POSIX priority/fork API unavailable",
)
def test_nice_is_inherited_by_forked_grandchildren(fresh_home, monkeypatch):
    """A fork()'d grandchild of the worker must also be nice 19.

    Second incident, 2026-09-20 11:31 (card t_2b81d847): a worker launched a
    Python script that ``os.fork()``ed 2*ncpu spinners for 2400s — five minutes
    after the first burn was killed. The burners are never the worker process
    itself, so deprioritising only the direct child would be theatre. Niceness
    survives both fork and exec, which is why setting it in the exec'd launcher
    covers the tree; this test pins that property rather than assuming it.
    """
    monkeypatch.setattr(kb, "worker_cpu_priority_config", lambda cfg=None: ("background", 19))
    captured = _spawn_and_capture_kwargs(fresh_home, monkeypatch, "t_fork")
    monkeypatch.undo()

    # A grandchild via fork(), and a great-grandchild via fork()+exec, both
    # report their own priority. Never a gateway — trivial python children.
    program = (
        "import os,sys,subprocess\n"
        "r,w=os.pipe()\n"
        "if os.fork()==0:\n"
        "    os.close(r)\n"
        "    forked=os.getpriority(os.PRIO_PROCESS,0)\n"
        "    execed=subprocess.run([sys.executable,'-c',\n"
        "        \"import os;print(os.getpriority(os.PRIO_PROCESS,0))\"],\n"
        "        capture_output=True,text=True).stdout.strip()\n"
        "    os.write(w,f'{forked} {execed}'.encode())\n"
        "    os._exit(0)\n"
        "os.close(w)\n"
        "print(os.read(r,64).decode())\n"
    )
    out = _launch_with_captured_prefix(captured, program, timeout=120)
    assert out.returncode == 0, out.stderr
    forked_nice, execed_nice = out.stdout.strip().split()
    assert int(forked_nice) == 19, f"fork()'d grandchild at nice {forked_nice}"
    assert int(execed_nice) == 19, f"exec'd great-grandchild at nice {execed_nice}"


def test_worker_cpu_priority_resolves_normal_by_default():
    """t_6ffdd0c4 (Ace 2026-10-19 21:52): the dev pipeline is first-class by default."""
    mode, nice = kb.worker_cpu_priority_config({})
    assert (mode, nice) == ("normal", 0)


def test_worker_cpu_priority_invalid_value_falls_back_to_normal():
    """A typo must never silently demote the dev pipeline ("unsure goes up")."""
    mode, nice = kb.worker_cpu_priority_config({"worker_cpu_priority": "turbo"})
    assert (mode, nice) == ("normal", 0)


def test_worker_cpu_priority_background_is_still_an_opt_in():
    assert kb.worker_cpu_priority_config({"worker_cpu_priority": "background"}) == ("background", 19)


def test_default_spawn_argv_has_no_taskpolicy_prefix_and_no_nice(fresh_home, monkeypatch):
    """RED on the pre-2026-10-19 default: the real resolver (no stub) on darwin with taskpolicy present
    must hand Popen the bare worker argv and a child at the dispatcher's own nice."""
    monkeypatch.setattr(kb.sys, "platform", "darwin")  # os-marker: ok — fork test pins the darwin branch as data so it runs on every lane
    monkeypatch.setattr(kb.os, "access", lambda path, flag: True)
    monkeypatch.setattr(kb, "worker_cpu_priority_config",
                        lambda cfg=None, _real=kb.worker_cpu_priority_config: _real({}))
    from hermes_cli import kanban_proc_bounds as kpb
    monkeypatch.setattr(kpb, "resolve_worker_nproc_limit", lambda *a, **k: None)
    captured = _spawn_and_capture_kwargs(fresh_home, monkeypatch, "t_default")
    assert "/usr/sbin/taskpolicy" not in captured["cmd"], captured["cmd"][:4]
    assert captured.get("preexec_fn") is None
    assert "--" not in captured["cmd"][:8], "default spawn must not renice the worker"


# ---------------------------------------------------------------------------
# (b) the knob set to 'normal' leaves priority untouched
# ---------------------------------------------------------------------------


def test_worker_cpu_priority_normal_opts_out():
    mode, nice = kb.worker_cpu_priority_config({"worker_cpu_priority": "normal"})
    assert mode == "normal"
    assert nice == 0


def test_normal_mode_installs_no_preexec_fn(fresh_home, monkeypatch):
    """With the knob at 'normal', the spawn must not touch child priority."""
    monkeypatch.setattr(
        kb, "worker_cpu_priority_config", lambda cfg=None: ("normal", 0)
    )
    # The process-count preexec (t_368e9873) is independent of priority.
    from hermes_cli import kanban_proc_bounds as kpb
    monkeypatch.setattr(kpb, "resolve_worker_nproc_limit", lambda *a, **k: None)
    captured = _spawn_and_capture_kwargs(fresh_home, monkeypatch, "t_normal")
    assert captured.get("preexec_fn") is None
    assert "--" not in captured["cmd"][:8], (
        "'normal' must leave the child at inherited priority"
    )


@pytest.mark.skipif(os.name != "posix", reason="POSIX RLIMIT_NPROC only")
def test_spawn_launcher_lowers_nproc_in_a_real_child(fresh_home, monkeypatch):
    """The launcher _default_spawn prefixes also lowers RLIMIT_NPROC soft AND
    hard (t_368e9873), even with priority at 'normal', with no preexec_fn."""
    resource = pytest.importorskip("resource")
    soft = resource.getrlimit(resource.RLIMIT_NPROC)[0]
    if soft != resource.RLIM_INFINITY and soft <= 777:
        pytest.skip("inherited limit already at/below the probe value")
    monkeypatch.setattr(
        kb, "worker_cpu_priority_config", lambda cfg=None: ("normal", 0)
    )
    from hermes_cli import kanban_proc_bounds as kpb
    monkeypatch.setattr(kpb, "resolve_worker_nproc_limit", lambda *a, **k: 777)
    captured = _spawn_and_capture_kwargs(fresh_home, monkeypatch, "t_nproc")
    assert captured.get("preexec_fn") is None
    monkeypatch.undo()  # real Popen again for the probe child
    out = _launch_with_captured_prefix(
        captured, "import os,resource;print(os.getpid(),resource.getrlimit(resource.RLIMIT_NPROC))",
        timeout=60)
    assert out.returncode == 0, out.stderr
    assert out.stdout.split(None, 1)[1].strip() == "(777, 777)"


def _bound_execute_child(captured: dict) -> dict:
    """The ``_execute_child`` arguments Popen derives from the captured call."""
    return dict(
        args=captured["cmd"], executable=None, preexec_fn=captured.get("preexec_fn"),
        close_fds=captured.get("close_fds", True), pass_fds=tuple(captured.get("pass_fds", ())),
        cwd=captured.get("cwd"), env=captured.get("env"), startupinfo=None, creationflags=0,
        shell=False, p2cread=-1, p2cwrite=-1, c2pread=-1, c2pwrite=-1, errread=-1, errwrite=-1,
        restore_signals=captured.get("restore_signals", True), gid=None, gids=None, uid=None,
        umask=-1, start_new_session=captured.get("start_new_session", False), process_group=-1,
    )


@pytest.mark.skipif(os.name != "posix", reason="POSIX spawn path only")
def test_nproc_limited_spawn_is_posix_spawn_eligible(fresh_home, monkeypatch):
    """t_b9a61253: with the NPROC ceiling AND nice active, the worker Popen must be
    expressible as one posix_spawn (spawn_nofork._plan != None), never the
    fork_exec fallback a preexec_fn forces. Pure decision check; runs on Linux CI."""
    import tools.spawn_nofork as nofork

    monkeypatch.setattr(kb, "worker_cpu_priority_config", lambda cfg=None: ("background", 19))
    from hermes_cli import kanban_proc_bounds as kpb
    monkeypatch.setattr(kpb, "resolve_worker_nproc_limit", lambda *a, **k: 777)
    captured = _spawn_and_capture_kwargs(fresh_home, monkeypatch, "t_plan")
    assert captured.get("preexec_fn") is None
    assert nofork._plan(_bound_execute_child(captured)) is not None
    # Mutant arm: the pre-t_b9a61253 shape (same call plus a preexec_fn) is refused.
    assert nofork._plan({**_bound_execute_child(captured), "preexec_fn": os.getpid}) is None


@pytest.mark.platforms("macos")  # the spawn_nofork hook installs on macOS only
def test_nproc_limited_spawn_takes_posix_spawn_not_fork_exec(fresh_home, monkeypatch):
    """Real spawn under the gateway's no-fork hook: zero fork_exec calls, at least
    one native posix_spawn, and the child reads the lowered RLIMIT_NPROC."""
    import _posixsubprocess
    import tools.spawn_nofork as nofork

    resource = pytest.importorskip("resource")
    soft = resource.getrlimit(resource.RLIMIT_NPROC)[0]
    if soft != resource.RLIM_INFINITY and soft <= 777:
        pytest.skip("inherited limit already at/below the probe value")
    monkeypatch.setattr(kb, "worker_cpu_priority_config", lambda cfg=None: ("normal", 0))
    from hermes_cli import kanban_proc_bounds as kpb
    monkeypatch.setattr(kpb, "resolve_worker_nproc_limit", lambda *a, **k: 777)
    captured = _spawn_and_capture_kwargs(fresh_home, monkeypatch, "t_hook")
    monkeypatch.undo()

    counts = {"native": 0, "fork": 0}
    real_native = nofork._native_spawn

    def native(*a, **k):
        rc, pid = real_native(*a, **k)
        counts["native"] += rc == 0
        return rc, pid

    owner = subprocess if hasattr(subprocess, "_fork_exec") else _posixsubprocess
    name = "_fork_exec" if owner is subprocess else "fork_exec"
    real_fork = getattr(owner, name)

    def fork(*a, **k):
        counts["fork"] += 1
        return real_fork(*a, **k)

    monkeypatch.setattr(nofork, "_native_spawn", native)
    monkeypatch.setattr(owner, name, fork)
    monkeypatch.delenv("HERMES_SPAWN_LEGACY", raising=False)
    assert nofork.install_process_hook()
    try:
        cmd = captured["cmd"]
        prefix = cmd[: cmd.index("--") + 1]
        proc = subprocess.Popen(
            [*prefix, sys.executable, "-c",
             "import os,resource;print(os.getpid(),resource.getrlimit(resource.RLIMIT_NPROC)[0])"],
            stdout=subprocess.PIPE, text=True, start_new_session=True,
        )
        out, _ = proc.communicate(timeout=60)
    finally:
        nofork.uninstall_process_hook()
    assert counts["fork"] == 0 and counts["native"] >= 1, counts
    pid, limit = out.split()
    assert int(pid) == proc.pid, "the launcher must exec in place (Popen.pid is the worker)"
    assert limit == "777"


@pytest.mark.skipif(os.name != "posix", reason="POSIX only")
def test_launcher_exec_failure_exits_127():
    from hermes_cli import kanban_proc_bounds as kpb

    out = subprocess.run([*kpb.worker_limits_launcher_argv(10**6, 0), "/nonexistent/worker"],
                         capture_output=True, text=True, timeout=60)
    assert out.returncode == 127 and "exec" in out.stderr


def test_launcher_prefix_is_empty_with_nothing_to_apply():
    from hermes_cli import kanban_proc_bounds as kpb

    assert kpb.worker_limits_launcher_argv(None, 0) == []
    assert kpb.worker_limits_launcher_argv(0, 0) == []


def test_config_yaml_knob_reaches_the_resolver(fresh_home, monkeypatch):
    """Round-trip through the real loader, not a hand-built dict.

    Guards the ``from_dict``/loader class of bug where a declared knob is read
    back at its default because nothing extracts it from the YAML section.
    """
    (fresh_home / "config.yaml").write_text(
        "kanban:\n  worker_cpu_priority: normal\n", encoding="utf-8"
    )
    from hermes_cli.config import load_config

    cfg = load_config()
    assert (cfg.get("kanban") or {}).get("worker_cpu_priority") == "normal"
    mode, nice = kb.worker_cpu_priority_config(cfg.get("kanban") or {})
    assert (mode, nice) == ("normal", 0)


def test_knob_is_declared_in_config_defaults():
    """`hermes config set kanban.worker_cpu_priority` must not cry wolf."""
    from hermes_cli.config_defaults import DEFAULT_CONFIG

    assert "worker_cpu_priority" in DEFAULT_CONFIG["kanban"]
    assert DEFAULT_CONFIG["kanban"]["worker_cpu_priority"] == "normal"


# ---------------------------------------------------------------------------
# (c) macOS: nice alone does not leave the gateway's QoS class (t_14c130aa)
# ---------------------------------------------------------------------------


def test_idle_mode_resolves_and_stays_niced():
    assert kb.worker_cpu_priority_config({"worker_cpu_priority": "idle"}) == ("idle", 19)


@pytest.mark.parametrize(
    "mode, expected",
    [
        ("background", ["/usr/sbin/taskpolicy", "-c", "utility"]),
        ("idle", ["/usr/sbin/taskpolicy", "-b"]),
        ("normal", []),
    ],
)
def test_darwin_prefix_per_mode(monkeypatch, mode, expected):
    monkeypatch.setattr(kb.sys, "platform", "darwin")  # os-marker: ok — fork test pins the darwin branch as data so it runs on every lane
    monkeypatch.setattr(kb.os, "access", lambda path, flag: True)
    assert kb.worker_darwin_qos_prefix(mode) == expected


def test_darwin_prefix_is_empty_off_macos(monkeypatch):
    monkeypatch.setattr(kb.sys, "platform", "linux")
    assert kb.worker_darwin_qos_prefix("background") == []


def test_darwin_prefix_degrades_when_taskpolicy_missing(monkeypatch):
    """A missing wrapper must fall back to nice-only, never break the spawn."""
    monkeypatch.setattr(kb.sys, "platform", "darwin")  # os-marker: ok — fork test pins the darwin branch as data so it runs on every lane
    monkeypatch.setattr(kb.os, "access", lambda path, flag: False)
    assert kb.worker_darwin_qos_prefix("background") == []


@pytest.mark.parametrize(
    "mode, expect_prefix",
    [("background", True), ("idle", True), ("normal", False)],
)
def test_spawn_argv_carries_the_darwin_prefix(fresh_home, monkeypatch, mode, expect_prefix):
    """The prefix must reach Popen, ahead of the unchanged worker argv."""
    monkeypatch.setattr(kb.sys, "platform", "darwin")  # os-marker: ok — fork test pins the darwin branch as data so it runs on every lane
    monkeypatch.setattr(kb.os, "access", lambda path, flag: True)
    nice = 0 if mode == "normal" else 19
    monkeypatch.setattr(kb, "worker_cpu_priority_config", lambda cfg=None: (mode, nice))
    cmd = _spawn_and_capture_kwargs(fresh_home, monkeypatch, f"t_{mode}")["cmd"]
    prefix = kb.worker_darwin_qos_prefix(mode) if expect_prefix else []
    assert cmd[: len(prefix)] == prefix
    assert "-p" in cmd[len(prefix):] and cmd[len(prefix)] != "/usr/sbin/taskpolicy"


_TASKPOLICY = "/usr/sbin/taskpolicy"


@pytest.mark.skipif(
    sys.platform != "darwin" or not os.access(_TASKPOLICY, os.X_OK),
    reason="macOS taskpolicy required",
)
def test_idle_prefix_puts_forked_and_execed_descendants_in_darwin_bg():
    """Real process tree: darwin-BG survives fork AND exec below the worker.

    PRIO_DARWIN_PROCESS is readable unprivileged, so this proves the
    property end-to-end rather than trusting the argv. (The utility clamp of
    'background' is only readable via root-only taskinfo; the fork/exec
    inheritance mechanism is the same task-policy inheritance.)
    """
    prio_darwin_process = 4
    program = (
        "import os,sys,subprocess\n"
        f"P={prio_darwin_process}\n"
        "r,w=os.pipe()\n"
        "if os.fork()==0:\n"
        "    os.close(r)\n"
        "    forked=os.getpriority(P,0)\n"
        "    execed=subprocess.run([sys.executable,'-c',\n"
        "        f'import os;print(os.getpriority({P},0))'],\n"
        "        capture_output=True,text=True).stdout.strip()\n"
        "    os.write(w,f'{forked} {execed}'.encode())\n"
        "    os._exit(0)\n"
        "os.close(w)\n"
        "print(os.getpriority(P,0), os.read(r,64).decode())\n"
    )
    out = subprocess.run(
        [*kb.worker_darwin_qos_prefix("idle"), sys.executable, "-c", program],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert out.returncode == 0, out.stderr
    assert out.stdout.split() == ["1", "1", "1"], out.stdout


@pytest.mark.skipif(
    sys.platform != "darwin" or not os.access(_TASKPOLICY, os.X_OK),
    reason="macOS taskpolicy required",
)
def test_taskpolicy_prefix_keeps_the_worker_pid():
    """Exec-form: the dispatcher tracks Popen.pid, so it must BE the worker."""
    proc = subprocess.Popen(
        [*kb.worker_darwin_qos_prefix("background"), sys.executable, "-c",
         "import os; print(os.getpid())"],
        stdout=subprocess.PIPE,
        text=True,
    )
    out, _ = proc.communicate(timeout=60)
    assert int(out.strip()) == proc.pid


# ---------------------------------------------------------------------------
# Spawn observability: one auditable line per worker
# ---------------------------------------------------------------------------


def test_spawn_logs_cpu_priority_line(fresh_home, monkeypatch, caplog):
    import logging

    caplog.set_level(logging.INFO, logger=kb._log.name)
    _spawn_and_capture_kwargs(fresh_home, monkeypatch, "t_logline")
    line = "\n".join(r.getMessage() for r in caplog.records)
    assert "PHASE=worker_spawn" in line
    assert "task=t_logline" in line
    assert "cpu_priority=" in line
    assert "nice=" in line
    assert "darwin_policy=" in line
