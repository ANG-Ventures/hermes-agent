"""A pool host whose worker would commit as an unadmitted git identity is refused at spawn (t_db46f06b).

10-27 23:04 ace-media's kanbanw ~/.gitconfig turned into ``ci <ci@example.invalid>``; every worker placed there
committed as it and hermes-home#7516 / #7520 were red on 10-live-git-identity 40 min later.

The probe is the worker's REAL terminal (kanban_worker_ident_probe: the worker's boot, ``_get_env_config``, the
ssh backend's ``resolve_passthrough_env`` + SendEnv + login shell), run in the card workspace. Hermetic: ``ssh`` on
PATH is tests/hermes_cli/fixtures/fake_ssh.sh, which runs the remote command locally with HOME at a fixture dir and
only the SendEnv names; the profile is a fixture HERMES_HOME; the policy is a fixture file.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_worker_hosts as kwh
from hermes_cli import kanban_worker_ident_probe as kwip
from hermes_cli import kanban_worker_pool as kwp

BOT = "333956806+ang-fleet-workers[bot]@users.noreply.github.com"
POLICY = ("identity_email_allowed() {\n  case \"$1\" in\n"
          f"    '9063726+Kyzcreig@users.noreply.github.com'|'{BOT}') return 0 ;;\n    *) return 1 ;;\n  esac\n}}\n")
FAKE_SSH = Path(__file__).parent / "fixtures" / "fake_ssh.sh"


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


class Rig:
    """A pool host (kanbanw HOME behind the fake ssh), one assignee profile and the card workspace."""

    def __init__(self, tmp_path):
        self.root = tmp_path
        self.remote = tmp_path / "kanbanw"
        self.remote.mkdir()
        self.profile = tmp_path / "vh" / "profiles" / "alpha"
        self.profile.mkdir(parents=True)
        (self.profile / "config.yaml").write_text("terminal:\n  env_passthrough: []\n")   # a live profile
        self.bin = tmp_path / "bin"
        self.bin.mkdir()
        shutil.copy(FAKE_SSH, self.bin / "ssh")
        (self.bin / "ssh").chmod(0o755)
        (self.bin / "scp").write_text("#!/bin/sh\nexit 0\n")
        (self.bin / "scp").chmod(0o755)
        self.board = tmp_path / "board"
        self.ws = self.board / "t_x"
        self.ws.mkdir(parents=True)

    def gitconfig(self, email, extra=""):
        (self.remote / ".gitconfig").write_text(f"[user]\n\tname = x\n\temail = {email}\n{extra}")

    def login_env(self, email):
        """worker-git-identity.sh's exported env, sourced by the login shell."""
        (self.remote / ".profile").write_text(f"export GIT_AUTHOR_NAME=bot GIT_AUTHOR_EMAIL='{email}' "
                                              f"GIT_COMMITTER_NAME=bot GIT_COMMITTER_EMAIL='{email}'\n")

    def profile_git(self, author_email, *, names=("GIT_AUTHOR_EMAIL",)):
        """The assignee profile forwards ``names`` (terminal.env_passthrough); the value lives in its .env."""
        (self.profile / "config.yaml").write_text(
            "terminal:\n  env_passthrough: [" + ", ".join(names) + "]\n")
        (self.profile / ".env").write_text(f"GIT_AUTHOR_EMAIL={author_email}\n")

    def env(self, host=None):
        """The env the dispatcher hands the worker: profile home bound, placement applied."""
        env = {"PATH": f"{self.bin}:/usr/bin:/bin", "HOME": str(self.root / "studio-home"),
               "HERMES_HOME": str(self.profile), "FAKE_SSH_HOME": str(self.remote), "TMPDIR": str(self.root)}
        kwh.apply_placement(env, host or _host(), str(self.ws), run_id=7)
        return env

    def check(self, *, host=None, board="default", runner=subprocess.run):
        kwh.check_worker_identity(host or _host(), str(self.ws), self.env(host), "alpha", board=board, runner=runner)


@pytest.fixture
def rig(tmp_path):
    return Rig(tmp_path)


def _counting(calls):
    def run(argv, **kw):
        calls.append(argv)
        return subprocess.run(argv, **kw)
    return run


def test_clobbered_gitconfig_refuses_the_host_and_names_the_value(rig, policy):
    rig.gitconfig("ci@example.invalid")
    with pytest.raises(kwh.WorkerGitIdentityRefused) as ei:
        rig.check()
    assert (ei.value.host, ei.value.board, ei.value.email) == ("ace-media", "default", "ci@example.invalid")
    assert "ace-media" in str(ei.value) and "ci@example.invalid" in str(ei.value)   # one line, host + value
    assert list(rig.ws.iterdir()) == []                                            # scratch probe repo removed


def test_admitted_identity_spawns(rig, policy):
    rig.gitconfig(BOT)
    rig.check()


def test_login_env_wins_over_a_clobbered_file(rig, policy):
    """The worker commits with worker-git-identity.sh's exported env: a bad file under a good env is fine..."""
    rig.gitconfig("t@example.invalid")
    rig.login_env(BOT)
    rig.check()


def test_bad_login_env_is_refused_even_with_a_good_file(rig, policy):
    """...and a bad env (the #5586 improvised identity) is refused even when the file is right."""
    rig.gitconfig(BOT)
    rig.login_env("daedalus@fleet")
    with pytest.raises(kwh.WorkerGitIdentityRefused) as ei:
        rig.check()
    assert ei.value.email == "daedalus@fleet"


def test_workspace_includeif_override_is_refused(rig, policy):
    """Prism e29ea5b7755d: the global identity is admitted, but ``includeIf "gitdir:..."`` for the board's
    workspaces overrides it with a refused one. The probe reads in the workspace, so the host is refused."""
    bad = rig.root / "bad.gitconfig"
    bad.write_text("[user]\n\temail = t@example.invalid\n")
    rig.gitconfig(BOT, f'[includeIf "gitdir:{rig.board}/"]\n\tpath = {bad}\n')
    with pytest.raises(kwh.WorkerGitIdentityRefused) as ei:
        rig.check()
    assert ei.value.email == "t@example.invalid"
    assert list(rig.ws.iterdir()) == []


def test_existing_workspace_repo_local_identity_is_read(rig, policy):
    """A continuation's workspace already is a repo: its own .git/config identity is what the worker commits as."""
    rig.gitconfig(BOT)
    env = {"HOME": str(rig.remote), "PATH": "/usr/bin:/bin", "GIT_CONFIG_NOSYSTEM": "1"}
    subprocess.run(["git", "init", "-q", str(rig.ws)], check=True, env=env)
    subprocess.run(["git", "-C", str(rig.ws), "config", "user.email", "daedalus@fleet"], check=True, env=env)
    with pytest.raises(kwh.WorkerGitIdentityRefused) as ei:
        rig.check()
    assert ei.value.email == "daedalus@fleet"
    assert sorted(p.name for p in rig.ws.iterdir()) == [".git"]


def test_profile_passthrough_identity_is_what_is_probed(rig, policy):
    """The worker's terminal forwards the profile's passthrough (.env value, SendEnv): an admitted forwarded author
    over a clobbered host file is fine, because that is what the worker commits as. No model: the real backend."""
    rig.gitconfig("t@example.invalid")
    (rig.remote / ".profile").write_text(f"export GIT_COMMITTER_NAME=bot GIT_COMMITTER_EMAIL='{BOT}'\n")
    rig.profile_git(BOT)
    rig.check()


def test_profile_sourced_refusal_is_a_card_fault_not_a_host_quarantine(rig, policy, monkeypatch):
    """Prism f57827c258e9 / 39ff2e41e51a / b381c849ec78: host admitted, the profile forwards a refused
    GIT_AUTHOR_EMAIL. Re-probed without the profile's git names the host is admitted, so the refusal names the
    profile and the variable, is NOT a host fault (retry charged), and quarantines / pages no host."""
    rig.gitconfig(BOT)
    rig.profile_git("daedalus@fleet")
    calls = []
    with pytest.raises(kwh.WorkerGitIdentityProfileRefused) as ei:
        rig.check(runner=_counting(calls))
    e = ei.value
    assert (e.host, e.email, e.profile, e.variable) == ("ace-media", "daedalus@fleet", "alpha", "GIT_AUTHOR_EMAIL")
    assert "alpha" in str(e) and "GIT_AUTHOR_EMAIL" in str(e) and "onboard" not in str(e)
    assert not isinstance(e, kwh.WorkerGitIdentityRefused)
    assert [("--without-profile-git" in a) for a in calls] == [False, True]          # probed twice
    noted = []
    monkeypatch.setattr(kwh, "note_identity_fault", lambda *a: noted.append(a))
    assert kbd._host_fault(e) is None
    assert noted == [] and not kwp.board_fault("ace-media", "default")


def test_bad_host_under_a_bad_profile_is_still_the_host(rig, policy):
    """Refused without the profile's names too: the host's fault, whatever the profile forwards."""
    rig.gitconfig("t@example.invalid")
    rig.profile_git("daedalus@fleet")
    with pytest.raises(kwh.WorkerGitIdentityRefused) as ei:
        rig.check()
    assert kbd._host_fault(ei.value) is not None and kwp.board_fault("ace-media", "default")


def test_admitted_identity_is_probed_once(rig, policy):
    rig.gitconfig(BOT)
    rig.profile_git(BOT)
    calls = []
    rig.check(runner=_counting(calls))
    assert len(calls) == 1


def test_unread_identity_never_refuses(rig, policy):
    rig.gitconfig("ci@example.invalid")
    for out in ("", "boot noise\n", kwip.MARK + "{not json\n", kwip.MARK + '{"emails": null}\n'):
        rig.check(runner=lambda argv, out=out, **kw: subprocess.CompletedProcess(argv, 1, out, "boom"))
    rig.check(runner=lambda argv, **kw: (_ for _ in ()).throw(subprocess.TimeoutExpired(argv, 1)))
    assert kwip.parse_output("@@RC=128\n") == [""]        # git refused: a read, never admitted
    assert kwip.parse_output("x <a@b> 1 +0000\n@@RC=0\n") is None
    assert kwip.parse_output("x <a@b> 1 +0000\ny <c@d> 1 -0700\n@@RC=0\n") == ["a@b", "c@d"]


def test_no_policy_file_or_local_hop_means_no_probe(rig, monkeypatch):
    rig.gitconfig("ci@example.invalid")
    calls = []
    monkeypatch.setattr(kwh, "identity_policy_path", lambda: None)
    rig.check(runner=_counting(calls))
    p = rig.root / "identity-policy.sh"
    p.write_text(POLICY)
    monkeypatch.setattr(kwh, "identity_policy_path", lambda: str(p))
    local = kwp.PoolHost(name=kwh.LOCAL_HOST_NAME, ssh_host="localhost", ssh_user="kanbanw", slots=1,
                         capacity_pct=1.0, absence="optional", profiles=(), state="active", enabled=True, priority=0)
    kwh.check_worker_identity(local, str(rig.ws), rig.env(), "alpha", runner=_counting(calls))
    assert calls == []


def test_policy_is_read_from_the_file_and_fails_closed(tmp_path):
    p = tmp_path / "identity-policy.sh"
    p.write_text(POLICY)
    assert kwh.identity_email_allowed(str(p), BOT)
    assert not kwh.identity_email_allowed(str(p), "t@example.invalid")
    assert not kwh.identity_email_allowed(str(tmp_path / "absent.sh"), BOT)


def test_policy_edit_takes_effect_without_restart(tmp_path):
    """Prism 583e98241dfc: no admission cache; a policy edit applies on the next check, both ways."""
    p = tmp_path / "identity-policy.sh"
    p.write_text(POLICY)
    assert kwh.identity_email_allowed(str(p), BOT)
    p.write_text("identity_email_allowed() { return 1; }\n")
    assert not kwh.identity_email_allowed(str(p), BOT)
    p.write_text(POLICY)
    assert kwh.identity_email_allowed(str(p), BOT)


def test_identity_fault_quarantines_the_host_board_and_pages_once(monkeypatch):
    from hermes_cli import kanban_budget
    sent = []
    monkeypatch.setattr(kanban_budget, "_notify_script_path", lambda home=None: "/x/notify.py")
    monkeypatch.setattr(kanban_budget, "_run_notify", lambda argv: sent.append(argv) or True)
    why = str(kwh.WorkerGitIdentityRefused("ace-media", "default", "ci@example.invalid"))
    assert kwh.note_identity_fault("ace-media", "default", why) is True
    assert kwh.note_identity_fault("ace-media", "house-voice", why) is False   # one page per HOST
    assert kwp.board_fault("ace-media", "default") and kwp.board_fault("ace-media", "house-voice")
    assert len(sent) == 1 and "ace-media" in sent[0][-1] and "ci@example.invalid" in sent[0][-1]


def test_failed_page_is_not_latched(monkeypatch):
    """Prism 5a786096bb00: a page that did not go out must not suppress the next one for the whole window."""
    from hermes_cli import kanban_budget
    results = [False, True]
    sent = []
    monkeypatch.setattr(kanban_budget, "_notify_script_path", lambda home=None: "/x/notify.py")
    monkeypatch.setattr(kanban_budget, "_run_notify", lambda argv: sent.append(argv) or results.pop(0))
    assert kwh.note_identity_fault("ace-media", "default", "why") is False
    assert kwh.note_identity_fault("ace-media", "default", "why") is True
    assert len(sent) == 2


def test_boardless_host_refusal_is_paged_and_charged(monkeypatch):
    """No board = no pair to quarantine: the refusal is paged and charged, never an uncharged retry every tick."""
    noted = []
    monkeypatch.setattr(kwh, "note_identity_fault", lambda *a: noted.append(a) or True)
    assert kbd._host_fault(kwh.WorkerGitIdentityRefused("ace-media", None, "ci@example.invalid")) is None
    assert noted and noted[0][0] == "ace-media"


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


def test_dispatch_profile_refusal_charges_the_card_and_quarantines_no_host(kanban_home, monkeypatch):
    """Prism f57827c258e9 end to end: a profile-sourced refusal is a charged spawn failure; no host is fenced."""
    from hermes_cli import kanban_budget
    pages = []
    monkeypatch.setattr(kanban_budget, "_notify_script_path", lambda home=None: "/x/notify.py")
    monkeypatch.setattr(kanban_budget, "_run_notify", lambda argv: pages.append(argv) or True)

    def spawn(task, workspace, *, board=None, placement=None):
        raise kwh.WorkerGitIdentityProfileRefused(placement.name, "default", "daedalus@fleet", "alpha",
                                                  "GIT_AUTHOR_EMAIL")

    boards = {"ace-media": {"default"}, "ace-ai": {"default"}}
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="card", assignee="alpha")
        kbd.dispatch_once(conn, spawn_fn=spawn, max_spawn=8, spawn_limit=0, spawn_paused="test",
                          spillover=_plan(boards), reconcile_orphans=False, failure_limit=3)
        task = kb.get_task(conn, tid)
        runs = conn.execute("SELECT outcome, metadata, error FROM task_runs WHERE task_id=?", (tid,)).fetchall()
    assert task.consecutive_failures == 1
    assert runs[0]["outcome"] == "spawn_failed" and '"infrastructure": true' not in (runs[0]["metadata"] or "")
    assert "alpha" in runs[0]["error"] and "GIT_AUTHOR_EMAIL" in runs[0]["error"]
    assert not kwp.board_fault("ace-media", "default") and not kwp.board_fault("ace-ai", "default")
    assert pages == []
