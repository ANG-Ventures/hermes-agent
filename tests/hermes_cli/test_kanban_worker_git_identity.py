"""A pool host whose worker shell would commit as an unadmitted git identity is refused at spawn (t_db46f06b).

10-27 23:04 ace-media's kanbanw ~/.gitconfig turned into ``ci <ci@example.invalid>``; every worker placed there
committed as it and hermes-home#7516 / #7520 were red on 10-live-git-identity 40 min later. The spawn's remote mkdir
now also reads ``git var`` in the worker's login shell and refuses an identity the fleet policy file does not admit:
a host fault (no retry charged, the card spills next tick), one page per host.

Hermetic: the remote command runs in a local ``sh -c`` with HOME at a fixture dir; the policy is a fixture file.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_worker_hosts as kwh
from hermes_cli import kanban_worker_pool as kwp

BOT = "333956806+ang-fleet-workers[bot]@users.noreply.github.com"
POLICY = ("identity_email_allowed() {\n  case \"$1\" in\n"
          f"    '9063726+Kyzcreig@users.noreply.github.com'|'{BOT}') return 0 ;;\n    *) return 1 ;;\n  esac\n}}\n")


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    kwp._board_faults.clear()
    kwh._identity_paged.clear()
    monkeypatch.setattr(kbd, "_default_spawn", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("real spawn")))
    yield
    kwp._board_faults.clear()
    kwh._identity_paged.clear()


@pytest.fixture
def policy(tmp_path, monkeypatch):
    p = tmp_path / "identity-policy.sh"
    p.write_text(POLICY)
    monkeypatch.setattr(kwh, "identity_policy_path", lambda: str(p))
    return p


def _host(name="ace-media"):
    return kwp.PoolHost(name=name, ssh_host=name, ssh_user="kanbanw", slots=4, capacity_pct=0.8,
                        absence="optional", profiles=("alpha",), state="active", enabled=True, priority=1)


def _worker_home(tmp_path, gitconfig_email=None, profile_email=None):
    """A kanbanw HOME: ~/.gitconfig (the file a fixture can clobber) and ~/.profile (worker-git-identity.sh's env)."""
    home = tmp_path / "kanbanw"
    home.mkdir(exist_ok=True)
    if gitconfig_email:
        (home / ".gitconfig").write_text(f"[user]\n\tname = x\n\temail = {gitconfig_email}\n")
    if profile_email:
        (home / ".profile").write_text(f"export GIT_AUTHOR_NAME=bot GIT_AUTHOR_EMAIL='{profile_email}' "
                                       f"GIT_COMMITTER_NAME=bot GIT_COMMITTER_EMAIL='{profile_email}'\n")
    return home


def _runner(home, calls=None):
    """Run the ssh remote command locally as the worker would: login profile from ``home``, no GIT_* inherited."""
    def run(argv, **kw):
        if calls is not None:
            calls.append(argv[-1])
        env = {"HOME": str(home), "PATH": "/usr/bin:/bin", "GIT_CONFIG_NOSYSTEM": "1"}
        # sshd with AcceptEnv: a SendEnv name reaches the remote shell with the CLIENT env's value.
        sent = [a.split("=", 1)[1] for a in argv if a.startswith("SendEnv=")]
        client = kw.get("env") or {}
        env.update({n: client[n] for n in sent if n in client})
        return subprocess.run(["sh", "-c", argv[-1]], capture_output=True, text=True, env=env, timeout=60)
    return run


def test_clobbered_gitconfig_refuses_the_host_and_names_the_value(tmp_path, policy):
    home = _worker_home(tmp_path, gitconfig_email="ci@example.invalid")
    ws = tmp_path / "board" / "t_x"
    ws.parent.mkdir()
    with pytest.raises(kwh.WorkerGitIdentityRefused) as ei:
        kwh.prepare_remote_workspace(_host(), str(ws), runner=_runner(home), board="default")
    assert (ei.value.host, ei.value.board, ei.value.email) == ("ace-media", "default", "ci@example.invalid")
    assert "ace-media" in str(ei.value) and "ci@example.invalid" in str(ei.value)   # one line, host + value
    assert ws.is_dir()                                                             # the mkdir itself still ran


def test_admitted_identity_spawns(tmp_path, policy):
    home = _worker_home(tmp_path, gitconfig_email=BOT)
    ws = tmp_path / "board" / "t_x"
    ws.parent.mkdir()
    kwh.prepare_remote_workspace(_host(), str(ws), runner=_runner(home), board="default")
    assert ws.is_dir()


def test_login_env_wins_over_a_clobbered_file(tmp_path, policy):
    """The worker commits with worker-git-identity.sh's exported env: a bad file under a good env is fine..."""
    home = _worker_home(tmp_path, gitconfig_email="t@example.invalid", profile_email=BOT)
    ws = tmp_path / "board" / "t_x"
    ws.parent.mkdir()
    kwh.prepare_remote_workspace(_host(), str(ws), runner=_runner(home), board="default")


def test_bad_login_env_is_refused_even_with_a_good_file(tmp_path, policy):
    """...and a bad env (the #5586 improvised identity) is refused even when the file is right."""
    home = _worker_home(tmp_path, gitconfig_email=BOT, profile_email="daedalus@fleet")
    (tmp_path / "board").mkdir()
    with pytest.raises(kwh.WorkerGitIdentityRefused) as ei:
        kwh.prepare_remote_workspace(_host(), str(tmp_path / "board" / "t_x"), runner=_runner(home), board="default")
    assert ei.value.email == "daedalus@fleet"


def test_no_identity_at_all_is_refused(tmp_path, policy):
    home = _worker_home(tmp_path)
    (tmp_path / "board").mkdir()
    env_probe = subprocess.run(["sh", "-c", "git var GIT_AUTHOR_IDENT"], capture_output=True, text=True,
                               env={"HOME": str(home), "PATH": "/usr/bin:/bin", "GIT_CONFIG_NOSYSTEM": "1"})
    if env_probe.returncode == 0:
        pytest.skip("this git synthesizes user@host without a configured identity")
    with pytest.raises(kwh.WorkerGitIdentityRefused) as ei:
        kwh.prepare_remote_workspace(_host(), str(tmp_path / "board" / "t_x"), runner=_runner(home), board="default")
    assert ei.value.email == ""


def test_unread_identity_never_refuses(tmp_path, policy):
    """A missing marker / a timed-out login shell proves nothing: never a refusal (only a READ, bad one is)."""
    for out in ("", "@@hermes-git-ident\n", "@@hermes-git-ident\nRC=124\n", "@@hermes-git-ident\nRC=127\n",
                "@@hermes-git-ident\nx <a@b> 1 +0000\nRC=0\n"):          # timeout, no bash, a half read
        assert kwh._ident_emails(out) is None, out
    assert kwh._ident_emails("@@hermes-git-ident\nRC=128\n") == [""]   # git refused: a read, never admitted
    run = lambda argv, **kw: subprocess.CompletedProcess(argv, 0, "@@hermes-git-ident\nRC=124\n", "")  # noqa: E731
    kwh.prepare_remote_workspace(_host(), "/w/default/t_x", runner=run, board="default")


def test_no_policy_file_means_no_identity_read(tmp_path, monkeypatch):
    """A non-fleet install has no identity-policy.sh: the mkdir is byte-identical to before."""
    monkeypatch.setattr(kwh, "identity_policy_path", lambda: None)
    calls = []
    kwh.prepare_remote_workspace(_host(), str(tmp_path / "t_x"), runner=_runner(_worker_home(tmp_path), calls))
    assert "git var" not in calls[0] and "&& exit 0;" in calls[0]


def test_local_uid_hop_is_not_probed(tmp_path, policy):
    calls = []
    local = kwp.PoolHost(name=kwh.LOCAL_HOST_NAME, ssh_host="localhost", ssh_user="_kanbanw", slots=0,
                         capacity_pct=1.0, absence="required", profiles=(), state="active", enabled=True, priority=0)
    kwh.prepare_remote_workspace(local, str(tmp_path / "t_x"), runner=_runner(_worker_home(tmp_path), calls))
    assert "git var" not in calls[0]


def test_policy_is_read_from_the_file_and_fails_closed(tmp_path):
    p = tmp_path / "p.sh"
    p.write_text('identity_email_allowed() { [ "$1" = only@x ]; }\n')
    assert kwh.identity_email_allowed(str(p), "only@x") and not kwh.identity_email_allowed(str(p), BOT)
    assert not kwh.identity_email_allowed(str(tmp_path / "missing.sh"), BOT)


def test_policy_edit_takes_effect_without_restart(tmp_path):
    """Prism 583e98241dfc: an admission must not outlive the policy that granted it (nor a denial)."""
    p = tmp_path / "p.sh"
    p.write_text('identity_email_allowed() { [ "$1" = only@x ]; }\n')
    assert kwh.identity_email_allowed(str(p), "only@x") and not kwh.identity_email_allowed(str(p), BOT)
    p.write_text(f'identity_email_allowed() {{ [ "$1" = "{BOT}" ]; }}\n')
    assert not kwh.identity_email_allowed(str(p), "only@x") and kwh.identity_email_allowed(str(p), BOT)


def test_identity_fault_quarantines_the_host_board_and_pages_once(monkeypatch):
    sent = []
    from hermes_cli import kanban_budget
    monkeypatch.setattr(kanban_budget, "_notify_script_path", lambda home=None: "/x/notify.py")
    monkeypatch.setattr(kanban_budget, "_run_notify", lambda argv: sent.append(argv) or True)
    why = str(kwh.WorkerGitIdentityRefused("ace-media", "default", "ci@example.invalid"))
    assert kwh.note_identity_fault("ace-media", "default", why) is True
    assert kwh.note_identity_fault("ace-media", "house-voice", why) is False   # one page per HOST
    assert kwp.board_fault("ace-media", "default") and kwp.board_fault("ace-media", "house-voice")
    assert len(sent) == 1 and "ace-media" in sent[0][-1] and "ci@example.invalid" in sent[0][-1]


def _passthrough(email):
    return {"GIT_AUTHOR_EMAIL": email, "GIT_COMMITTER_EMAIL": email}, set()


def test_forwarded_passthrough_identity_is_what_is_probed(tmp_path, policy):
    """Prism 63185217be5e: the worker's terminal forwards terminal.env_passthrough with SendEnv, so a forwarded
    admitted identity over a clobbered ~/.gitconfig is fine, and the value never rides the remote command text."""
    home = _worker_home(tmp_path, gitconfig_email="t@example.invalid")
    (tmp_path / "board").mkdir()
    calls = []
    kwh.prepare_remote_workspace(_host(), str(tmp_path / "board" / "t_x"), runner=_runner(home, calls),
                                 board="default", git_env=_passthrough(BOT))
    assert BOT not in calls[0]
    with pytest.raises(kwh.WorkerGitIdentityRefused):    # the same host without the passthrough is refused
        kwh.prepare_remote_workspace(_host(), str(tmp_path / "board" / "t_y"), runner=_runner(home), board="default")


def test_forwarded_bad_identity_is_refused_over_a_good_file(tmp_path, policy):
    home = _worker_home(tmp_path, gitconfig_email=BOT)
    (tmp_path / "board").mkdir()
    with pytest.raises(kwh.WorkerGitIdentityRefused) as ei:
        kwh.prepare_remote_workspace(_host(), str(tmp_path / "board" / "t_x"), runner=_runner(home),
                                     board="default", git_env=_passthrough("ci@example.invalid"))
    assert ei.value.email == "ci@example.invalid"


def test_scoped_unset_runs_after_the_login_profile(tmp_path, policy):
    """A scoped name the worker's terminal unsets is unset AFTER the profile exported it, as prepend_unset does."""
    home = _worker_home(tmp_path, gitconfig_email="t@example.invalid", profile_email=BOT)
    (tmp_path / "board").mkdir()
    with pytest.raises(kwh.WorkerGitIdentityRefused) as ei:
        kwh.prepare_remote_workspace(_host(), str(tmp_path / "board" / "t_x"), runner=_runner(home),
                                     board="default", git_env=({}, {"GIT_AUTHOR_EMAIL", "GIT_COMMITTER_EMAIL"}))
    assert ei.value.email == "t@example.invalid"


def test_worker_git_env_resolves_like_the_worker(tmp_path, monkeypatch):
    """Only git identity names from the passthrough list; worker env (+ process_env_files overlay) beats .env."""
    from agent import secret_scope
    monkeypatch.setattr(secret_scope, "is_multiplex_active", lambda: False)
    overlay = tmp_path / "ident.sh"
    overlay.write_text(f"export GIT_COMMITTER_EMAIL='{BOT}'\n")
    cfg = {"agent": {"process_env_files": [str(overlay)]}}
    vals, unset = kwh.worker_git_env({"GIT_AUTHOR_EMAIL": "a@x", "PATH": "/usr/bin:/bin"}, cfg,
                                     dotenv={"GIT_AUTHOR_EMAIL": "dotenv@x", "EMAIL": "e@x"},
                                     passthrough=["GIT_AUTHOR_EMAIL", "GIT_COMMITTER_EMAIL", "EMAIL", "GITHUB_TOKEN"])
    assert vals == {"GIT_AUTHOR_EMAIL": "a@x", "GIT_COMMITTER_EMAIL": BOT, "EMAIL": "e@x"} and unset == set()
    assert kwh.worker_git_env({}, None, dotenv={}, passthrough=["GITHUB_TOKEN"]) == ({}, set())
    monkeypatch.setattr(secret_scope, "is_multiplex_active", lambda: True)
    assert kwh.worker_git_env({}, None, dotenv={}, passthrough=["GIT_AUTHOR_EMAIL"]) == ({}, {"GIT_AUTHOR_EMAIL"})


def test_dispatch_resolves_git_env_under_the_assignee_profile(tmp_path, policy, monkeypatch):
    """_default_spawn's helper: the profile's passthrough + overlay + .env, inside the assignee scope; None when off."""
    import contextlib
    from agent import secret_scope
    from hermes_cli import config as vconfig
    from tools import env_passthrough
    scopes = []
    monkeypatch.setattr(kbd, "_worker_profile_scope", lambda home, **k: scopes.append(home) or contextlib.nullcontext())
    monkeypatch.setattr(vconfig, "load_config", lambda: {})
    monkeypatch.setattr(vconfig, "load_env", lambda: {"GIT_AUTHOR_EMAIL": "dot@x"})
    monkeypatch.setattr(env_passthrough, "get_all_passthrough", lambda: frozenset({"GIT_AUTHOR_EMAIL", "GITHUB_TOKEN"}))
    monkeypatch.setattr(secret_scope, "is_multiplex_active", lambda: False)
    assert kbd._worker_git_env("/p/alpha", {"PATH": "/usr/bin"}) == ({"GIT_AUTHOR_EMAIL": "dot@x"}, set())
    assert kbd._worker_git_env("/p/alpha", {"GIT_AUTHOR_EMAIL": "env@x"}) == ({"GIT_AUTHOR_EMAIL": "env@x"}, set())
    assert scopes == ["/p/alpha", "/p/alpha"]
    assert kbd._worker_git_env(None, {}) is None
    monkeypatch.setattr(kwh, "identity_policy_path", lambda: None)
    assert kbd._worker_git_env("/p/alpha", {}) is None


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    monkeypatch.setattr(kbd, "_system_memory_sample", lambda: {}, raising=False)
    monkeypatch.setattr("hermes_cli.profiles.profile_exists", lambda name: True, raising=False)
    (home / "profiles" / "alpha").mkdir(parents=True, exist_ok=True)
    with kb.connect_closing():
        kb.create_board(slug="default", name="Test")
    return home


def _plan(boards):
    """The test_kanban_board_hostable harness: two pool hosts, ace-media first."""
    hosts = [kwp.PoolHost(name=n, ssh_host=n, ssh_user=kwp.SSH_USER, slots=2, capacity_pct=0.8, absence="required",
                          profiles=("alpha",), state="active", enabled=True, priority=i)
             for i, n in enumerate(("ace-media", "ace-ai"))]
    p = kwp.plan(hosts, {}, probe=lambda h: kwp.HostSample(1.0, 16, boards=frozenset(boards[h.name])))
    p.band, p.spill_reason = "paused", "load"
    return p


def test_dispatch_refusal_consumes_no_retry_and_spills_next_tick(kanban_home, monkeypatch):
    """End to end: the identity refusal on ace-media closes the run as infrastructure (0 retries) and the next tick
    places the card on ace-ai."""
    from hermes_cli import kanban_budget
    pages = []
    monkeypatch.setattr(kanban_budget, "_notify_script_path", lambda home=None: "/x/notify.py")
    monkeypatch.setattr(kanban_budget, "_run_notify", lambda argv: pages.append(argv) or True)
    placed = []

    def spawn(task, workspace, *, board=None, placement=None):
        if placement.name == "ace-media":
            raise kwh.WorkerGitIdentityRefused("ace-media", "default", "ci@example.invalid")
        placed.append((task.id, placement.name))
        return 4242

    boards = {"ace-media": {"default"}, "ace-ai": {"default"}}
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="card", assignee="alpha")
        for _ in range(4):  # more ticks than failure_limit
            res = kbd.dispatch_once(conn, spawn_fn=spawn, max_spawn=8, spawn_limit=0, spawn_paused="test",
                                    spillover=_plan(boards), reconcile_orphans=False, failure_limit=3)
            if placed:
                break
        task = kb.get_task(conn, tid)
        runs = conn.execute("SELECT outcome, metadata FROM task_runs WHERE task_id=? ORDER BY id", (tid,)).fetchall()
    assert placed == [(tid, "ace-ai")], res
    assert task.consecutive_failures == 0
    assert runs[0]["outcome"] == "spawn_failed" and '"infrastructure": true' in runs[0]["metadata"]
    assert '"git_identity_refused": "ci@example.invalid"' in runs[0]["metadata"]
    assert len(pages) == 1
