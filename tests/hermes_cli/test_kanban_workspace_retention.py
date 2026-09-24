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


def test_live_home_card_without_cwd_in_candidate_reaps_it(kanban_home, monkeypatch):
    _home_rooted_dir_card(kanban_home, "running")
    old = _scratch(_mktask("old done"), "done", finished_days_ago=5)
    # cwds that must NOT count: the workspaces root (an ancestor), a sibling
    # whose name merely starts with the candidate's, and the home itself.
    _fake_lsof(monkeypatch, _listing(
        "/", str(kanban_home), str(kb.workspaces_root()),
        str(old.parent / (old.name + "x")),
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
    reported = kb._kernel_path(nested) or nested
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
    monkeypatch.setattr(kb, "_kernel_path", boom)
    assert kb._process_cwd_within(tmp_path) is True


@pytest.mark.parametrize("row_spelling", ["env", "disk"])
def test_gc_retains_candidate_when_home_env_is_spelled_differently(
        tmp_path, monkeypatch, row_spelling):
    """Argus's E2E repro: HERMES_HOME in another case than the on-disk home.

    ``disk`` also stores the dir:home row in the OTHER spelling from the
    candidates, so the home-rooted exemption must still recognise it (the
    free control is reaped) while the live cwd still retains."""
    on_disk = tmp_path / "home_dir"
    on_disk.mkdir()
    spelled = tmp_path / "HOME_DIR"
    if not spelled.exists():
        pytest.skip("case-sensitive filesystem")
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
