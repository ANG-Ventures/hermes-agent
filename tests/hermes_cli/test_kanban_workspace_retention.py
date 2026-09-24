"""Done-card scratch workspaces must actually be reclaimed (card t_b99d8978).

Measured on the Mac Studio 2026-09-23: 307 ``done`` cards still held their
scratch workspaces (65 GB) because completion cleanup was REFUSED for nearly
all of them. The workspace-deletion audit named the reason on every line:
``owner-has-live-run owner=<ambiguous-owner>,t_…`` -- nine ``dir:`` cards
whose workspace was the kanban home itself (``dir:~/.hermes``) counted as
"owners" of every scratch dir beneath it. With one of them live the refusal
was owner-has-live-run; with none live it was <ambiguous-owner>. Either way
nothing was removed, and ``kanban gc`` only ever looked at ``archived`` rows,
so nothing retried.

These tests drive the real lanes against a sealed temp home.
"""

from __future__ import annotations

import argparse
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from hermes_cli import kanban as kanban_cli
from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _mktask(title: str) -> str:
    with kb.connect_closing() as conn:
        return kb.create_task(conn, title=title, assignee="daedalus")


def _set(task_id: str, **cols) -> None:
    keys = ", ".join(f"{k}=?" for k in cols)
    with kb.connect_closing() as conn:
        conn.execute(f"UPDATE tasks SET {keys} WHERE id=?", (*cols.values(), task_id))
        conn.commit()


def _scratch(task_id: str, status: str, *, finished_days_ago: float = 0.0) -> Path:
    root = kb.workspaces_root()
    root.mkdir(parents=True, exist_ok=True)
    ws = root / task_id
    ws.mkdir()
    _set(task_id, status=status, workspace_kind="scratch", workspace_path=str(ws),
         completed_at=int(time.time() - finished_days_ago * 86400))
    return ws


def _home_rooted_dir_card(home: Path, status: str) -> str:
    """A ``dir:`` card whose workspace is the kanban home -- the live shape."""
    tid = _mktask(f"dir card on the home ({status})")
    extra = {"claim_expires": int(time.time()) + 3600} if status == "running" else {}
    _set(tid, status=status, workspace_kind="dir", workspace_path=str(home), **extra)
    return tid


@pytest.mark.parametrize("dir_card_states", [("running",), ("done", "done"),
                                             ("running", "done", "blocked")])
def test_home_rooted_dir_cards_do_not_own_scratch_workspaces(kanban_home, dir_card_states):
    for st in dir_card_states:
        _home_rooted_dir_card(kanban_home, st)
    tid = _mktask("finished scratch card")
    ws = _scratch(tid, "done")

    assert kb._live_owners_of_path(ws) == []
    with kb.connect_closing() as conn:
        kb._cleanup_workspace(conn, tid)
    assert not ws.exists(), "completion cleanup still refused by a home-rooted dir card"


def test_enclosing_scratch_card_is_still_an_owner(kanban_home):
    """Control: the exemption is only for paths ABOVE the workspaces root.

    A live card whose own scratch dir encloses the target still protects it.
    """
    live = _mktask("live enclosing card")
    enclosing = _scratch(live, "running")
    _set(live, claim_expires=int(time.time()) + 3600)
    victim = enclosing / "repo"
    victim.mkdir()
    (victim / "work.txt").write_text("unretained\n", encoding="utf-8")
    caller = _mktask("idle caller")
    _set(caller, status="archived")

    assert kb._live_owners_of_path(victim) == [live]
    assert not kb.safe_remove_workspace_dir(victim, task_id=caller, reason="t")
    assert (victim / "work.txt").exists()


def test_custom_dir_under_the_root_still_owns_nested_paths(kanban_home):
    """Control: a stored path strictly under the root (not task-named) is managed
    storage and keeps owning what is below it."""
    root = kb.workspaces_root()
    root.mkdir(parents=True, exist_ok=True)
    custom = root / "custom"
    (custom / "repo").mkdir(parents=True)
    live = _mktask("live custom-dir card")
    _set(live, status="running", workspace_kind="dir", workspace_path=str(custom),
         claim_expires=int(time.time()) + 3600)
    assert kb._live_owners_of_path(custom / "repo") == [live]


def _gc(**kw):
    return kanban_cli._cmd_gc(argparse.Namespace(**kw))


def test_gc_reaps_old_done_workspaces_and_spares_every_protected_state(kanban_home):
    _home_rooted_dir_card(kanban_home, "running")
    old_done = _scratch(_mktask("old done"), "done", finished_days_ago=4)
    fresh_done = _scratch(_mktask("fresh done"), "done", finished_days_ago=1)
    protected = {}
    for st in ("running", "review", "blocked", "ready", "todo", "triage"):
        tid = _mktask(f"old {st}")
        protected[st] = _scratch(tid, st, finished_days_ago=30)
        if st == "running":
            _set(tid, claim_expires=int(time.time()) + 3600)

    assert _gc(done_retention_days=3) == 0

    assert not old_done.exists()
    assert fresh_done.is_dir()
    for st, ws in protected.items():
        assert ws.is_dir(), f"gc removed a {st} card's workspace"
    audit = kb.workspace_deletion_log_path().read_text(encoding="utf-8")
    assert f"\tDELETE\ttask={old_done.name}\t" in audit, "no ledger line for the reaped workspace"


@pytest.mark.parametrize("parent_kind", ["scratch", "worktree"])
def test_gc_defers_linked_parent_until_child_finishes(kanban_home, parent_kind):
    parent = _mktask("parent handoff")
    ws = _scratch(parent, "done", finished_days_ago=5)
    _set(parent, workspace_kind=parent_kind)
    (ws / "handoff.txt").write_text("still needed", encoding="utf-8")
    child = _mktask("active child")
    _set(child, status="running", workspace_kind="dir", workspace_path=str(kanban_home),
         claim_expires=int(time.time()) + 3600)
    with kb.connect_closing() as conn:
        kb.link_tasks(conn, parent, child)
    assert _gc(done_retention_days=3) == 0
    assert (ws / "handoff.txt").read_text(encoding="utf-8") == "still needed"
    assert "active-children-need-handoff" in kb.workspace_deletion_log_path().read_text(encoding="utf-8")
    if parent_kind == "scratch":
        _set(child, status="done", claim_expires=None)
        assert _gc(done_retention_days=3) == 0
        assert not ws.exists()


def test_live_home_dir_card_using_scratch_cwd_blocks_gc(kanban_home):
    home_card = _home_rooted_dir_card(kanban_home, "running")
    old = _scratch(_mktask("old done"), "done", finished_days_ago=5)
    free = _scratch(_mktask("free old done"), "done", finished_days_ago=5)
    sleeper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"],
                               cwd=old, stdin=subprocess.DEVNULL,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        assert sleeper.poll() is None
        assert kb._live_owners_of_path(old) == [home_card]
        assert _gc(done_retention_days=3) == 0
        assert old.is_dir() and not free.exists()
    finally:
        sleeper.terminate()
        sleeper.wait(timeout=5)


def test_gc_done_retention_negative_disables_the_done_sweep(kanban_home):
    old_done = _scratch(_mktask("old done"), "done", finished_days_ago=10)
    assert _gc(done_retention_days=-1) == 0
    assert old_done.is_dir()


def test_gc_dry_run_deletes_nothing_and_lists_candidates(kanban_home, capsys):
    old_done = _scratch(_mktask("old done"), "done", finished_days_ago=10)
    archived = _scratch(_mktask("archived"), "archived", finished_days_ago=10)
    log_dir = kb.worker_logs_dir()
    log_dir.mkdir(parents=True, exist_ok=True)
    old_log = log_dir / "t_deadbeef.log"
    old_log.write_text("x\n", encoding="utf-8")
    import os
    past = time.time() - 400 * 86400
    os.utime(old_log, (past, past))

    assert _gc(done_retention_days=3, dry_run=True) == 0

    out = capsys.readouterr().out
    assert old_done.is_dir() and archived.is_dir() and old_log.exists()
    assert str(old_done) in out and str(archived) in out
    assert "2 workspace candidate(s)" in out


# -- one cached process-cwd scan per gc run (card t_ee808d83) ---------------
#
# The per-path ``lsof +D <workspace>`` probe walked every candidate tree and
# took 42-55 s on real 62k-205k-entry workspaces, over its 30 s timeout. The
# replacement is ONE machine-wide ``lsof -d cwd`` per gc run, prefix-matched in
# Python. These pin the three behaviours that matter: a failed scan retains,
# an unrelated cwd reaps, and a nested cwd retains.


def _fake_lsof(monkeypatch, behaviour):
    """Route only ``lsof`` argv through *behaviour*; count the calls."""
    real_run = subprocess.run
    calls = []

    def run(argv, *a, **kw):
        if argv and argv[0] == "lsof":
            calls.append(list(argv))
            return behaviour(argv)
        return real_run(argv, *a, **kw)

    monkeypatch.setattr(kb.subprocess, "run", run)
    return calls


def _listing(*cwds):
    out = "".join(f"p{100 + i}\nfcwd\nn{c}\n" for i, c in enumerate(cwds))
    return lambda argv: subprocess.CompletedProcess(argv, 0, stdout=out, stderr="")


def _raise(exc):
    def behaviour(argv):
        raise exc
    return behaviour


@pytest.mark.parametrize("failure", ["timeout", "missing-binary", "nonzero-exit", "empty"])
def test_cwd_scan_failure_retains_every_candidate(kanban_home, monkeypatch, failure):
    behaviour = {
        "timeout": _raise(subprocess.TimeoutExpired(["lsof"], 30)),
        "missing-binary": _raise(FileNotFoundError("lsof")),
        "nonzero-exit": lambda argv: subprocess.CompletedProcess(
            argv, 1, stdout="p1\nfcwd\nn/\n", stderr="lsof: boom"),
        "empty": lambda argv: subprocess.CompletedProcess(argv, 0, stdout="", stderr=""),
    }[failure]
    calls = _fake_lsof(monkeypatch, behaviour)
    home_card = _home_rooted_dir_card(kanban_home, "running")
    a = _scratch(_mktask("old done a"), "done", finished_days_ago=5)
    b = _scratch(_mktask("old done b"), "done", finished_days_ago=5)

    assert kb._process_cwd_within(a) is True
    assert kb._live_owners_of_path(a) == [home_card]
    assert _gc(done_retention_days=3) == 0

    assert a.is_dir() and b.is_dir(), "a failed cwd scan must fail CLOSED"
    assert calls, "the cwd scan was never attempted"
    audit = kb.workspace_deletion_log_path().read_text(encoding="utf-8")
    assert f"REFUSED\ttask={a.name}\t" in audit and "owner-has-live-run" in audit


@pytest.mark.skipif(not hasattr(signal, "setitimer"), reason="POSIX interval timer")
def test_cwd_stat_timeout_retains_candidate(kanban_home, monkeypatch):
    """The GC budget includes the realpath/stat pass, not only lsof."""
    _home_rooted_dir_card(kanban_home, "running")
    old = _scratch(_mktask("old done"), "done", finished_days_ago=5)
    _fake_lsof(monkeypatch, _listing("/", str(old.parent)))
    monkeypatch.setattr(kb, "_CWD_SCAN_BUDGET_SECONDS", 0.05)
    real_identity = kb._path_identity

    def stalled_identity(path, memo=None):
        if path == old and signal.getitimer(signal.ITIMER_REAL)[0] > 0:
            time.sleep(2)
        return real_identity(path, memo)

    monkeypatch.setattr(kb, "_path_identity", stalled_identity)
    started = time.monotonic()
    assert _gc(done_retention_days=3) == 0
    assert time.monotonic() - started < 1.5
    assert old.is_dir(), "a stalled stat must fail CLOSED"
    audit = kb.workspace_deletion_log_path().read_text(encoding="utf-8")
    assert f"REFUSED\ttask={old.name}\t" in audit and "owner-has-live-run" in audit


def test_cwd_budget_expired_after_scan_retains_candidate(kanban_home, monkeypatch):
    _home_rooted_dir_card(kanban_home, "running")
    old = _scratch(_mktask("old done"), "done", finished_days_ago=5)
    _fake_lsof(monkeypatch, _listing("/"))
    with kb.process_cwd_snapshot_scope():
        kb._process_cwds()
        token = kb._CWD_SCAN_DEADLINE.set(time.monotonic() - 1)
        try:
            assert kb._process_cwd_within(old) is True
        finally:
            kb._CWD_SCAN_DEADLINE.reset(token)
    assert old.is_dir()


def test_live_home_card_without_cwd_in_candidate_reaps_it(kanban_home, monkeypatch):
    _home_rooted_dir_card(kanban_home, "running")
    old = _scratch(_mktask("old done"), "done", finished_days_ago=5)
    # cwds that must NOT count: the workspaces root (an ancestor), a sibling
    # whose name merely starts with the candidate's, and the home itself.
    sibling = old.parent / (old.name + "x")
    sibling.mkdir()
    _fake_lsof(monkeypatch, _listing(
        "/", str(kanban_home), str(kb.workspaces_root()), str(sibling),
    ))
    assert kb._process_cwd_within(old) is False
    assert _gc(done_retention_days=3) == 0
    assert not old.exists()


def test_nested_cwd_in_candidate_retains_it(kanban_home, monkeypatch):
    home_card = _home_rooted_dir_card(kanban_home, "running")
    old = _scratch(_mktask("old done"), "done", finished_days_ago=5)
    free = _scratch(_mktask("free old done"), "done", finished_days_ago=5)
    nested = old / "repo" / "src" / "pkg"
    nested.mkdir(parents=True)
    # Real lsof prints the kernel's spelling of a cwd; mirror that.
    reported = nested
    _fake_lsof(monkeypatch, _listing("/", str(reported)))

    assert kb._live_owners_of_path(old) == [home_card]
    assert _gc(done_retention_days=3) == 0
    assert old.is_dir() and not free.exists()


def test_real_nested_cwd_process_retains_candidate(kanban_home):
    """Same contract against the real ``lsof`` and a real process."""
    _home_rooted_dir_card(kanban_home, "running")
    old = _scratch(_mktask("old done"), "done", finished_days_ago=5)
    free = _scratch(_mktask("free old done"), "done", finished_days_ago=5)
    nested = old / "a" / "b"
    nested.mkdir(parents=True)
    sleeper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"],
                               cwd=nested, stdin=subprocess.DEVNULL,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        assert sleeper.poll() is None
        assert _gc(done_retention_days=3) == 0
        assert old.is_dir() and not free.exists()
    finally:
        sleeper.terminate()
        sleeper.wait(timeout=5)


def test_gc_run_scans_process_cwds_once(kanban_home, monkeypatch):
    _home_rooted_dir_card(kanban_home, "running")
    ws = [_scratch(_mktask(f"old done {i}"), "done", finished_days_ago=5) for i in range(4)]
    calls = _fake_lsof(monkeypatch, _listing("/"))
    assert _gc(done_retention_days=3) == 0
    assert not any(w.exists() for w in ws)
    assert len(calls) == 1, calls
    assert "+D" not in calls[0], "per-path tree walk is back"


# -- spelling-blind cwd match (card t_ee808d83, Argus round 1) ---------------
#
# ``Path.resolve()`` follows symlinks only: on case-insensitive APFS it keeps
# the caller's case, NFC vs NFD, and the /System/Volumes/Data firmlink
# spelling, while lsof prints the kernel's canonical name. A string compare of
# the two failed OPEN and gc deleted a workspace a live process was inside.
# Every arm below uses a REAL process and the real lsof.


def _sleeper(cwd: Path) -> subprocess.Popen:
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"],
                            cwd=cwd, stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(0.2)
    assert proc.poll() is None
    return proc


def _stop(proc: subprocess.Popen) -> None:
    proc.terminate()
    proc.wait(timeout=5)


def _variant(real: Path, axis: str) -> Path:
    import unicodedata

    if axis == "case":
        alt = real.parent / real.name.upper()
    elif axis == "nfc":
        alt = real.parent / unicodedata.normalize("NFC", real.name)
    else:  # firmlink
        alt = Path("/System/Volumes/Data" + str(real))
    if str(alt) == str(real) or not alt.exists():
        pytest.skip(f"filesystem does not alias the {axis} spelling here")
    return alt


@pytest.mark.parametrize("axis", ["case", "nfc", "firmlink"])
def test_cwd_probe_matches_candidate_spelled_differently(tmp_path, axis):
    import unicodedata

    base = tmp_path.resolve()
    name = unicodedata.normalize("NFD", "t_caf\u00e9") if axis == "nfc" else "t_abc123"
    real = base / name
    (real / "sub").mkdir(parents=True)
    sibling = base / (name + "x")
    sibling.mkdir()
    query = _variant(real, axis)
    proc = _sleeper(real / "sub")
    try:
        assert kb._process_cwd_within(query) is True, f"{axis} spelling failed open"
        assert kb._process_cwd_within(sibling) is False, "sibling negative control"
    finally:
        _stop(proc)


def test_cwd_probe_fails_closed_when_candidate_cannot_be_named(monkeypatch, tmp_path):
    def boom(p):
        raise PermissionError(p)

    _fake_lsof(monkeypatch, _listing("/"))
    monkeypatch.setattr(kb, "_path_identity", boom)
    assert kb._process_cwd_within(tmp_path) is True


def _home_spellings(tmp_path: Path, axis: str) -> tuple[Path, Path]:
    """(on-disk home, the same directory spelled on *axis*)."""
    import unicodedata

    base = tmp_path.resolve()
    if axis == "nfc":
        on_disk = base / unicodedata.normalize("NFD", "home_caf\u00e9")
        on_disk.mkdir()
        spelled = base / unicodedata.normalize("NFC", "home_caf\u00e9")
    else:
        on_disk = base / "home_dir"
        on_disk.mkdir()
        spelled = _variant(on_disk, axis)
    if str(spelled) == str(on_disk) or not spelled.exists():
        pytest.skip(f"filesystem does not alias the {axis} spelling here")
    return on_disk, spelled


@pytest.mark.parametrize("axis", ["case", "nfc", "firmlink"])
@pytest.mark.parametrize("row_spelling", ["env", "disk"])
def test_gc_retains_candidate_when_home_env_is_spelled_differently(
        tmp_path, monkeypatch, row_spelling, axis):
    """Argus's E2E repro: HERMES_HOME spelled differently from the on-disk home.

    ``disk`` also stores the dir:home row in the OTHER spelling from the
    candidates, so the home-rooted exemption must still recognise it (the
    free control is reaped) while the live cwd still retains. Every spelling
    axis the pre-filter folds (case, NFC/NFD, the Data firmlink) has an arm,
    so dropping any one fold fails here (Argus round 2 mutants T3/T4)."""
    on_disk, spelled = _home_spellings(tmp_path, axis)
    monkeypatch.setenv("HERMES_HOME", str(spelled))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    _home_rooted_dir_card(spelled if row_spelling == "env" else on_disk, "running")
    old = _scratch(_mktask("old done"), "done", finished_days_ago=10)
    free = _scratch(_mktask("free old done"), "done", finished_days_ago=10)
    (old / "sub").mkdir()
    (old / "sub" / "work.txt").write_text("unretained\n", encoding="utf-8")
    proc = _sleeper(old / "sub")
    try:
        assert _gc(done_retention_days=3) == 0
        assert (old / "sub" / "work.txt").exists(), "gc deleted a live process's cwd"
        assert not free.exists(), "control: the unrelated candidate is still reaped"
    finally:
        _stop(proc)
    audit = kb.workspace_deletion_log_path().read_text(encoding="utf-8")
    assert f"REFUSED\ttask={old.name}\t" in audit and "owner-has-live-run" in audit


def test_owner_row_spelled_differently_still_owns(kanban_home):
    """Class sweep: a stored workspace_path in another case is the same dir."""
    root = kb.workspaces_root()
    root.mkdir(parents=True, exist_ok=True)
    custom = root / "custom"
    (custom / "repo").mkdir(parents=True)
    alt = _variant(custom, "case")
    live = _mktask("live custom-dir card, other spelling")
    _set(live, status="running", workspace_kind="dir", workspace_path=str(alt),
         claim_expires=int(time.time()) + 3600)
    assert kb._live_owners_of_path(custom / "repo") == [live]


def test_nested_live_dir_card_stored_in_other_case_retains_candidate(kanban_home):
    """A LIVE card's dir workspace nested INSIDE an old DONE candidate, stored in
    another case: gc must keep the candidate (Argus round 2 mutant C3)."""
    cand = _mktask("old done")
    ws = _scratch(cand, "done", finished_days_ago=10)
    nested = ws / "repo"
    nested.mkdir()
    (nested / "live_work.txt").write_text("live card's work\n", encoding="utf-8")
    stored = _variant(nested, "case")
    stored = Path(str(stored.parent).replace(cand, cand.upper())) / stored.name
    if not stored.is_dir():
        pytest.skip("case-sensitive filesystem")
    live = _mktask("live nested dir card")
    _set(live, status="running", workspace_kind="dir", workspace_path=str(stored),
         claim_expires=int(time.time()) + 3600)
    assert _gc(done_retention_days=3) == 0
    assert (nested / "live_work.txt").exists(), "gc deleted a live card's workspace"


@pytest.mark.parametrize("axis", ["root-case", "firmlink", "leaf-case", "stored-path"])
def test_gc_convention_owner_in_other_spelling_retains_live_work(kanban_home, axis):
    """A live scratch card without a stored path still owns its named directory."""
    live = _mktask("live card without stored path")
    root = kb.workspaces_root()
    directory = root / live
    directory.mkdir(parents=True)
    work = directory / "work.txt"
    work.write_text("live work", encoding="utf-8")
    _set(live, status="running", workspace_kind="scratch", workspace_path=None,
         claim_expires=int(time.time()) + 3600)
    done = _mktask("old row pointing at the live directory")
    if axis == "root-case":
        spelled = _variant(root, "case") / live.upper()
    elif axis == "firmlink":
        spelled = _variant(root, "firmlink") / live.upper()
    else:
        spelled = root / live.upper()
    if not spelled.is_dir():
        pytest.skip("filesystem does not alias this spelling")
    _set(done, status="done", workspace_kind="scratch",
         workspace_path=str(directory if axis == "stored-path" else spelled),
         completed_at=int(time.time() - 10 * 86400))
    assert _gc(done_retention_days=3) == 0
    assert work.read_text(encoding="utf-8") == "live work"
    assert "owner-has-live-run" in kb.workspace_deletion_log_path().read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def case_sensitive_volume():
    """Real case-sensitive APFS: the ordinary macOS temp volume is insensitive."""
    import shutil
    import os
    import tempfile

    if sys.platform != "darwin" or not shutil.which("hdiutil"):
        pytest.skip("requires macOS hdiutil")
    with tempfile.TemporaryDirectory(prefix="kanban-cs-", dir="/tmp") as temp:
        image = Path(temp) / "case-sensitive.dmg"
        mount = Path(f"/Volumes/KANBAN-CS-{os.getpid()}")
        subprocess.run(["hdiutil", "create", "-size", "64m", "-fs", "Case-sensitive APFS",
                        "-volname", "KANBAN-CS", str(image)], check=True,
                       stdin=subprocess.DEVNULL, capture_output=True)
        subprocess.run(["hdiutil", "attach", "-nobrowse", "-mountpoint", str(mount),
                        str(image)], check=True, stdin=subprocess.DEVNULL, capture_output=True)
        try:
            yield mount
        finally:
            subprocess.run(["hdiutil", "detach", str(mount)], check=True,
                           stdin=subprocess.DEVNULL, capture_output=True)


def test_gc_never_deletes_distinct_case_sensitive_directory(case_sensitive_volume, monkeypatch):
    home = case_sensitive_volume / "home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    root = case_sensitive_volume / "ws"
    root.mkdir()
    other = case_sensitive_volume / "WS"
    other.mkdir()
    monkeypatch.setenv("HERMES_KANBAN_WORKSPACES_ROOT", str(root))
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    kb.init_db()
    tid = _mktask("unmanaged case-sensitive directory")
    candidate = other / tid
    candidate.mkdir()
    # Both sides exist: a spelling-fold mutant must not borrow the managed
    # counterpart's identity for this distinct unmanaged directory.
    (root / tid).mkdir()
    work = candidate / "precious.txt"
    work.write_text("retain", encoding="utf-8")
    _set(tid, status="done", workspace_kind="scratch", workspace_path=str(candidate),
         completed_at=int(time.time() - 10 * 86400))
    assert _gc(done_retention_days=3) == 0
    assert work.read_text(encoding="utf-8") == "retain"
    assert not kb._is_managed_scratch_path(candidate)


def test_gc_reaps_managed_directory_on_case_sensitive_mount(case_sensitive_volume, monkeypatch):
    home = case_sensitive_volume / "control-home"
    home.mkdir()
    root = case_sensitive_volume / "control-ws"
    root.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_WORKSPACES_ROOT", str(root))
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    kb.init_db()
    tid = _mktask("managed case-sensitive control")
    candidate = root / tid
    candidate.mkdir()
    _set(tid, status="done", workspace_kind="scratch", workspace_path=str(candidate),
         completed_at=int(time.time() - 10 * 86400))
    assert _gc(done_retention_days=3) == 0
    assert not candidate.exists()


def test_case_sensitive_mount_boundary_does_not_inherit_parent_case_rule(
        case_sensitive_volume):
    root = case_sensitive_volume / "ws"
    root.mkdir(exist_ok=True)
    other = case_sensitive_volume / "WS"
    other.mkdir(exist_ok=True)
    assert not kb._same_tree(other, root)
    assert kb._same_tree(root / "missing-child", root) is False
    # The parent /Users volume is insensitive; it must not govern lookup
    # inside a mounted case-sensitive volume.
    assert kb._path_identity(other) != kb._path_identity(root)


def test_missing_stored_owner_path_can_only_refuse_candidate(kanban_home):
    old = _scratch(_mktask("old done"), "done", finished_days_ago=10)
    free = _scratch(_mktask("unrelated done"), "done", finished_days_ago=10)
    missing = old / "future-work"
    live = _mktask("live path not yet created")
    _set(live, status="running", workspace_kind="dir", workspace_path=str(missing),
         claim_expires=int(time.time()) + 3600)
    assert _gc(done_retention_days=3) == 0
    assert old.exists(), "an unknown stored owner must not authorize deletion"
    assert not free.exists(), "an unknown owner must not pin unrelated candidates"


def test_case_sensitive_distinct_root_is_not_managed(kanban_home):
    root = kb.workspaces_root()
    root.mkdir(parents=True)
    other = root.parent / root.name.upper()
    if other.exists():
        pytest.skip("case-insensitive filesystem")
    other.mkdir()
    candidate = other / "t_deadbeef"
    candidate.mkdir()
    assert not kb._is_managed_scratch_path(candidate)
    assert not kb._same_tree(candidate, root)


def test_scratch_row_stored_in_other_case_is_reaped(kanban_home):
    """A DONE scratch row whose workspace_path is spelled in another case is
    still managed storage: gc reclaims it instead of refusing it forever."""
    tid = _mktask("old done, other-case row")
    ws = _scratch(tid, "done", finished_days_ago=10)
    alt = _variant(ws, "case")
    _set(tid, workspace_path=str(alt))
    assert _gc(done_retention_days=3) == 0
    assert not ws.exists()


@pytest.mark.parametrize("axis", ["case", "firmlink"])
def test_workspaces_root_in_other_spelling_is_never_managed(kanban_home, axis):
    """Strict descendancy survives spelling-blind matching: the root itself,
    however spelled, is not a deletable scratch dir."""
    root = kb.workspaces_root()
    (root / "t_child").mkdir(parents=True)
    alt_root = _variant(root.resolve(), axis)
    assert kb._is_managed_scratch_path(alt_root) is False
    assert kb._is_managed_scratch_path(alt_root / "t_child") is True
    assert kb._is_managed_scratch_path(alt_root.parent / "logs") is False


def test_artifact_spelled_in_other_case_is_still_copied(kanban_home):
    """Completion artifacts declared under another-case spelling of the scratch
    workspace are preserved before cleanup (Argus round 2 mutant C2)."""
    tid = _mktask("artifact card")
    ws = _scratch(tid, "running")
    (ws / "report.txt").write_text("deliverable\n", encoding="utf-8")
    alt = _variant(ws, "case") / "report.txt"
    with kb.connect_closing() as conn:
        kb._persist_scratch_completion_artifacts(conn, tid, {"artifacts": [str(alt)]})
    copies = list(kb.task_attachments_dir(tid).glob("report*.txt"))
    assert copies and copies[0].read_text(encoding="utf-8") == "deliverable\n"


def test_audit_log_escapes_target_spelled_in_other_case(kanban_home):
    """Removing the kanban home spelled in another case must not route the audit
    line into the tree being removed (Argus round 2 mutant C6)."""
    target = _variant(kanban_home.resolve(), "case")
    log = kb._durable_audit_log_path(target.resolve(strict=False), None)
    assert not kb._same_tree(log.resolve(strict=False), kanban_home.resolve()), log


# --- Pin guards: an EQUIVALENT pin agrees; a native pin in a sandbox refuses ---
# Argus round 2 N1: the hazard predicate became spelling-blind while both
# agree-checks in front of it still compared strings, so the same kanban.db in
# another spelling raised KanbanPinDivergenceError and gc reclaimed nothing.


def _pin_env(monkeypatch, tmp_path, *, home: Path, pin: Path) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_DB", str(pin))
    monkeypatch.delenv("HERMES_KANBAN_SANDBOX", raising=False)
    monkeypatch.setattr(kb, "_PIN_AT_IMPORT", "")
    monkeypatch.setattr(kb, "_CHECKED_OVERRIDE_ESCAPES", set())
    monkeypatch.setattr(kb, "_CHECKED_PIN_BOARD_CONTRADICTIONS", set())


@pytest.mark.parametrize("axis", ["case", "firmlink"])
def test_equivalent_pin_is_accepted_and_gc_reaps(tmp_path, monkeypatch, axis):
    home = tmp_path.resolve() / ".hermes"
    home.mkdir()
    pin = _variant(home, axis) / "kanban.db"
    _pin_env(monkeypatch, tmp_path, home=home, pin=pin)
    assert kb._pin_file_agrees(kb.kanban_db_path(), home / "kanban.db")
    assert kb._pin_file_agrees(kb.kanban_db_path("default"), home / "kanban.db")
    kb.init_db()
    a = _scratch(_mktask("old done a"), "done", finished_days_ago=10)
    b = _scratch(_mktask("old done b"), "done", finished_days_ago=10)
    assert _gc(done_retention_days=3) == 0
    assert not a.exists() and not b.exists(), "equivalent pin blocked reclamation"


@pytest.mark.parametrize("axis", ["case", "firmlink"])
def test_conn_on_board_file_in_other_spelling_is_that_board(kanban_home, axis):
    """_conn_is_board asks the connection's file; the same file in another
    spelling is the same board, not a reason to open a second connection."""
    import sqlite3

    real = kb.kanban_db_path("default").resolve()
    conn = sqlite3.connect(str(_variant(real, axis)))
    try:
        assert kb._conn_is_board(conn, "default") is True
    finally:
        conn.close()
    other = sqlite3.connect(str(real.parent / "not-the-board.db"))
    try:
        assert kb._conn_is_board(other, "default") is False, "negative control"
    finally:
        other.close()


def test_noncanonical_pin_identity_preserves_sqlite_shared_lock(tmp_path, monkeypatch):
    """Identity must not open/close the DB file: POSIX close cancels SQLite locks."""
    import sqlite3

    home = tmp_path.resolve() / ".hermes"
    home.mkdir()
    real = home / "kanban.db"
    pin = _variant(home, "case") / "kanban.db"
    _pin_env(monkeypatch, tmp_path, home=home, pin=pin)
    kb.init_db()
    conn = sqlite3.connect(str(pin), isolation_level=None)
    conn.execute("PRAGMA journal_mode=DELETE")
    # A separate process attempts an EXCLUSIVE writer lock. Opening and closing
    # a descriptor in *this* process cancels its own SQLite POSIX read lock.
    check = (
        "import sqlite3, sys; c=sqlite3.connect(sys.argv[1], timeout=0); "
        "\ntry: c.execute('BEGIN EXCLUSIVE'); print('acquired')"
        "\nexcept sqlite3.OperationalError: print('locked')"
        "\nfinally: c.rollback(); c.close()"
    )

    def lock_type():
        return subprocess.check_output([sys.executable, "-c", check, str(real)],
                                       stdin=subprocess.DEVNULL, text=True).strip()

    try:
        conn.execute("BEGIN")
        conn.execute("SELECT name FROM sqlite_master").fetchall()
        assert lock_type() == "locked", "lock probe did not acquire SHARED"
        kb._conn_is_board(conn, "default")
        assert lock_type() == "locked"
        kb._refuse_if_override_escapes_hermes_home(pin)
        assert lock_type() == "locked"
        kb._refuse_if_pin_contradicts_board_arg("default", pin)
        assert lock_type() == "locked"
        kb._pin_divergence_is_a_hazard(pin)
        assert lock_type() == "locked"
    finally:
        conn.close()


def test_sandbox_with_native_pin_in_other_case_still_refuses(tmp_path, monkeypatch):
    """Positive control (incident shape, Argus round 2 C1): a sandboxed home
    whose pin reaches the machine's native home in another case must RAISE."""
    native = tmp_path.resolve() / ".hermes"
    native.mkdir()
    sandbox = tmp_path.resolve() / "sandbox"
    sandbox.mkdir()
    pin = _variant(native, "case") / "kanban.db"
    _pin_env(monkeypatch, tmp_path, home=sandbox, pin=pin)
    with pytest.raises(kb.KanbanPinDivergenceError):
        kb.kanban_db_path()
    with pytest.raises(kb.KanbanPinDivergenceError):
        kb.kanban_db_path("default")
