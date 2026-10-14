"""Two lifecycle invariants, using real SQLite and a local GitHub HTTP contract."""
import json
import os
import subprocess
import sys
import threading
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli.kanban_db_connect import connect


@pytest.fixture
def github(tmp_path, monkeypatch):
    state = {"conclusion": "success", "head": "a" * 40, "reads": 0, "requests": []}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            state["requests"].append(self.path)
            sha = state["head"]
            if self.path == "/graphql":
                query = self.headers.get("X-Query", "")
                if "branchProtectionRule" in query and state.get("app_token"):
                    # The fleet's GitHub App token lacks Administration:read: GitHub answers
                    # HTTP 200 with a FORBIDDEN field error (measured, t_b5b407c8).
                    value = {"data": {"repository": {"pullRequest": {"baseRef": {"branchProtectionRule": None}}}},
                             "errors": [{"type": "FORBIDDEN", "message": "Resource not accessible by integration"}]}
                else:
                    value = {"data": {"repository": {"pullRequest": {
                        "headRefOid": sha, "baseRefName": "main", "state": state.get("pr_state", "OPEN"),
                        "baseRef": {"branchProtectionRule": {"requiredStatusChecks": [
                            {"context": "required", "app": {"databaseId": 1}}]}}}}}}
            elif self.path.endswith("/branches/main"):
                if state.get("protection_denied"):
                    self.send_response(403)
                    self.end_headers()
                    self.wfile.write(b'{"message": "Resource not accessible by integration"}')
                    return
                value = {"name": "main", "protection": {"required_status_checks": {
                    "checks": [{"context": "required", "app_id": state.get("app_id", 1)}],
                    "contexts": ["required"]}}}
            elif "/issues/" in self.path and self.path.split("?")[0].endswith("/comments"):
                value = [state.get("comments", [])]
            elif "/rules/branches/" in self.path:
                value = [[]]
            elif "/check-runs" in self.path:
                run = {"id": 42, "name": "required", "head_sha": sha,
                       "app": {"id": 1}, "status": "in_progress" if state["conclusion"] == "pending" else "completed", "conclusion": state["conclusion"],
                       "html_url": "https://github.com/acme/repo/actions/runs/42"}
                if state.get("stale"):
                    run["head_sha"] = "b" * 40
                runs = [] if state.get("missing") else [run]
                value = [{"total_count": 100 + len(runs), "check_runs": [
                    {**run, "id": 1000 + i, "name": "optional", "conclusion": "skipped"}
                    for i in range(100)]}, {"total_count": 100 + len(runs), "check_runs": runs}]
                if state.get("race"):
                    state["race"]()
                if state.get("head_change"):
                    state["head"] = "b" * 40
            elif "/statuses" in self.path:
                value = [[{**st, "sha": sha} for st in state.get("statuses", [])]]
            elif self.path.startswith("/apps/"):
                # GET /apps/{slug}: the bot login's app id (public endpoint).
                value = {"id": state.get("apps", {}).get(self.path[len("/apps/"):]), "slug": self.path[6:]}
            elif "/pulls/" in self.path:
                merged = state.get("pr_state") == "MERGED"
                value = {"head": {"sha": sha}, "state": "closed" if merged else "open", "merged": merged,
                         "merged_by": {"login": state.get("merged_by", "operator")} if merged else None,
                         "base": {"ref": "main", "repo": {"full_name": state.get("canonical", "acme/repo")}}}
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.end_headers()
            self.wfile.write(json.dumps(value).encode())

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    shim = tmp_path / "bin"
    shim.mkdir()
    gh = shim / "gh"
    # Behaves like gh at the boundary the gate sees: an HTTP error or a GraphQL ``errors``
    # payload exits 1 with gh's own stderr line.
    gh.write_text(f"#!{sys.executable}\nimport json,sys,urllib.request,urllib.error\n"
                  f"u='http://127.0.0.1:{server.server_port}/'+sys.argv[2]\n"
                  "q=next((a[6:] for a in sys.argv if a.startswith('query=')), '')\n"
                  "req=urllib.request.Request(u, headers={'X-Query': ' '.join(q.split())})\n"
                  "try:\n    body=urllib.request.urlopen(req).read().decode()\n"
                  "except urllib.error.HTTPError as e:\n"
                  "    msg=json.loads(e.read().decode()).get('message','')\n"
                  "    sys.stderr.write(f'gh: {msg} (HTTP {e.code})\\n'); sys.exit(1)\n"
                  "print(body)\n"
                  "errs=json.loads(body).get('errors') if body.startswith('{') else None\n"
                  "if errs:\n    sys.stderr.write('gh: '+errs[0]['message']+'\\n'); sys.exit(1)\n")
    gh.chmod(0o755)
    monkeypatch.setenv("PATH", str(shim) + os.pathsep + os.environ["PATH"])
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    kb.init_db()
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.mark.platforms("linux")
def test_pr_completion_requires_current_required_evidence(github):
    with connect() as conn:
        for conclusion in ("failure", "pending", "cancelled", "timed_out", "action_required", "neutral", "skipped", None, "success"):
            github.update(conclusion=conclusion, head="a" * 40)
            tid = kb.create_task(conn, title="Publish", completion_contract="acme/repo")
            ok = kb.complete_task(conn, tid, result="done", metadata={"published_pr": "https://github.com/acme/repo/pull/7"})
            assert ok is (conclusion == "success")
            task = kb.get_task(conn, tid)
            assert (task.status == "done") is ok
            receipts = [json.loads(r[0]) for r in conn.execute(
                "SELECT payload FROM task_events WHERE task_id=? AND kind='pr_acceptance'", (tid,))]
            assert receipts and receipts[-1]["head_sha"] == "a" * 40
            if not ok:
                assert task.status in {"running", "ready", "blocked", "review"}
                assert "retry" in receipts[-1]["recovery"]
                assert receipts[-1]["checks"][0]["id"] == 42
        for fault in ("missing", "stale", "head_change"):
            github.update(conclusion="success", head="a" * 40)
            github[fault] = True
            tid = kb.create_task(conn, title=fault, completion_contract="acme/repo")
            assert not kb.complete_task(conn, tid, result="done", metadata={"published_pr": "https://github.com/acme/repo/pull/7"})
            assert kb.get_task(conn, tid).status != "done"
            github.pop(fault)
        # Omission and a sibling repository cannot downgrade the stored declaration.
        tid = kb.create_task(conn, title="publish", completion_contract="acme/repo")
        assert not kb.complete_task(conn, tid, summary="local green")
        assert not kb.complete_task(conn, tid, result="done", metadata={"published_pr": "https://github.com/other/repo/pull/7"})
        before = len(github["requests"])
        local = kb.create_task(conn, title="local", completion_contract="local-only")
        assert kb.complete_task(conn, local, summary="https://github.com/acme/repo/pull/7 is background context")
        assert len(github["requests"]) == before


@pytest.mark.platforms("linux")
def test_acceptance_receipts_and_terminal_write_share_run_ownership(github):
    with connect() as conn:
        for conclusion in ("success", "failure"):
            tid = kb.create_task(conn, title="race", completion_contract="acme/repo")
            owner = kb.claim_task(conn, tid)
            run_id = owner.current_run_id
            def reclaim():
                with connect() as rival:
                    assert kb.block_task(rival, tid, reason="Reassigned during acceptance")
                    assert kb.unblock_task(rival, tid)
                    github["replacement"] = kb.claim_task(rival, tid).current_run_id
            github.update(conclusion=conclusion, race=reclaim)
            assert not kb.complete_task(conn, tid, result="done", expected_run_id=run_id,
                metadata={"published_pr": "https://github.com/acme/repo/pull/7"})
            assert kb.get_task(conn, tid).current_run_id == github["replacement"]
            assert github["replacement"] != run_id
            assert kb.get_task(conn, tid).status != "done"
            assert conn.execute("SELECT count(*) FROM task_events WHERE task_id=? AND kind='pr_acceptance'", (tid,)).fetchone()[0] == 0
            github.pop("race")


# --- #122689: acceptance must read the repo as the ASSIGNEE profile's gh login ---

@pytest.mark.platforms("posix")
def test_acceptance_runs_gh_as_the_assignee_profile(tmp_path, monkeypatch):
    """The gh child env carries the assignee's own GH credentials (its .env),
    never the ambient/launch residue, and an invisible repo is classified
    `auth` naming the repository — not a retryable `infra` failure."""
    from pathlib import Path

    launch_home = tmp_path / "home"
    launch_home.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(launch_home))
    assignee_home = launch_home / "profiles" / "b"
    assignee_home.mkdir(parents=True)
    (assignee_home / ".env").write_text("GH_TOKEN=b-token\n", encoding="utf-8")
    # Ambient residue that must NOT decide the login.
    monkeypatch.setenv("GH_TOKEN", "launch-token")
    monkeypatch.setenv("GH_CONFIG_DIR", "/nonexistent/launch/gh")

    env_dump = tmp_path / "gh_env.json"
    shim = tmp_path / "bin"
    shim.mkdir()
    gh = shim / "gh"
    gh.write_text(f"#!{sys.executable}\nimport json, os\n"
                  f"json.dump(dict(os.environ), open({str(env_dump)!r}, 'w'))\n"
                  "print(json.dumps({'data': {'repository': None}}))\n")
    gh.chmod(0o755)
    monkeypatch.setenv("PATH", str(shim) + os.pathsep + os.environ["PATH"])
    kb.init_db()
    with connect() as conn:
        tid = kb.create_task(conn, title="as-b", completion_contract="acme/repo", assignee="b")
        assert not kb.complete_task(conn, tid, result="done",
                                    metadata={"published_pr": "https://github.com/acme/repo/pull/7"})
        assert kb.get_task(conn, tid).status != "done"
        receipts = [json.loads(r[0]) for r in conn.execute(
            "SELECT payload FROM task_events WHERE task_id=? AND kind='pr_acceptance'", (tid,))]
        assert receipts[-1]["classification"] == "auth"
        assert "acme/repo" in receipts[-1]["detail"]
    captured = json.loads(env_dump.read_text())
    assert captured["GH_TOKEN"] == "b-token"
    assert captured.get("GH_CONFIG_DIR") != "/nonexistent/launch/gh"
    assert "credentials" in (kb.get_task(conn, tid).last_failure_error or "")


@pytest.mark.platforms("posix")
def test_assignee_without_own_gh_login_never_falls_through_to_ambient_login(tmp_path, monkeypatch):
    """An assignee profile with no GH_TOKEN/GH_CONFIG_DIR of its own must not inherit the
    launch user's ~/.config/gh (HOME/XDG_CONFIG_HOME stay the launch process's): gh is pinned
    to a profile-owned config dir, its 'not logged in' exit is classified `auth` naming the profile."""
    launch_home = tmp_path / "home"
    launch_home.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(launch_home))
    assignee_home = launch_home / "profiles" / "b"
    assignee_home.mkdir(parents=True)
    (assignee_home / ".env").write_text("", encoding="utf-8")
    monkeypatch.setenv("GH_TOKEN", "launch-token")
    monkeypatch.setenv("GH_CONFIG_DIR", "/nonexistent/launch/gh")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "launch-xdg"))

    env_dump = tmp_path / "gh_env.json"
    shim = tmp_path / "bin"
    shim.mkdir()
    gh = shim / "gh"
    # Real gh: GH_CONFIG_DIR wins; a config dir without hosts.yml means "not logged in" (exit 4).
    gh.write_text(f"#!{sys.executable}\nimport json, os, pathlib, sys\n"
                  f"pathlib.Path({str(env_dump)!r}).write_text(json.dumps(dict(os.environ)), encoding='utf-8')\n"
                  "if 'GH_CONFIG_DIR' in os.environ and not os.path.exists(os.environ['GH_CONFIG_DIR']):\n"
                  "    sys.exit(4)\n"
                  "print(json.dumps({'data': {'repository': None}}))\n")
    gh.chmod(0o755)
    monkeypatch.setenv("PATH", str(shim) + os.pathsep + os.environ["PATH"])
    kb.init_db()
    with connect() as conn:
        tid = kb.create_task(conn, title="as-b", completion_contract="acme/repo", assignee="b")
        assert not kb.complete_task(conn, tid, result="done",
                                    metadata={"published_pr": "https://github.com/acme/repo/pull/7"})
        receipt = json.loads(conn.execute(
            "SELECT payload FROM task_events WHERE task_id=? AND kind='pr_acceptance'", (tid,)).fetchone()[0])
    assert receipt["classification"] == "auth"
    assert "'b'" in receipt["detail"] and "no login" in receipt["detail"]
    captured = json.loads(env_dump.read_text(encoding="utf-8-sig"))
    assert captured["GH_CONFIG_DIR"] == str(assignee_home / "gh")
    assert "GH_TOKEN" not in captured and "GITHUB_TOKEN" not in captured


def test_assigned_card_with_unresolvable_profile_is_auth_not_ambient(tmp_path, monkeypatch):
    """A card assigned to a profile that no longer exists must not run gh as the completing
    process's ambient login: classification `auth` naming the profile, gh never invoked."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("PATH", str(tmp_path / "empty-bin"))  # any gh spawn would fail as infra
    kb.init_db()
    with connect() as conn:
        tid = kb.create_task(conn, title="as-ghost", completion_contract="acme/repo", assignee="ghost")
        assert not kb.complete_task(conn, tid, result="done",
                                    metadata={"published_pr": "https://github.com/acme/repo/pull/7"})
        receipt = json.loads(conn.execute(
            "SELECT payload FROM task_events WHERE task_id=? AND kind='pr_acceptance'", (tid,)).fetchone()[0])
    assert receipt["classification"] == "auth"
    assert "'ghost'" in receipt["detail"] and "cannot be resolved" in receipt["detail"]


# --- human-lane sentinel: the review lane (``human:apollo``) has no profile by design ---

@pytest.mark.platforms("posix")
@pytest.mark.parametrize("assignee", ["human:apollo", "human"])
def test_human_review_lane_reads_with_the_ambient_login(tmp_path, monkeypatch, assignee):
    """A review-lane card (assignee ``human:<name>`` / ``human``) is completed by the operator:
    gh runs with the ambient env (no profile overlay), the receipt records the human-lane login,
    and a successful read is never classified `auth` for want of a profile."""
    launch_home = tmp_path / "home"
    launch_home.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(launch_home))
    monkeypatch.setenv("GH_TOKEN", "operator-token")
    env_dump = tmp_path / "gh_env.json"
    shim = tmp_path / "bin"
    shim.mkdir()
    gh = shim / "gh"
    gh.write_text(f"#!{sys.executable}\nimport json, os\n"
                  f"json.dump(dict(os.environ), open({str(env_dump)!r}, 'w'))\n"
                  "print(json.dumps({'data': {'repository': None}}))\n")
    gh.chmod(0o755)
    monkeypatch.setenv("PATH", str(shim) + os.pathsep + os.environ["PATH"])
    kb.init_db()
    with connect() as conn:
        tid = kb.create_task(conn, title="review-lane", completion_contract="acme/repo",
                             assignee=assignee)
        kb.complete_task(conn, tid, result="done",
                         metadata={"published_pr": "https://github.com/acme/repo/pull/7"})
        receipt = json.loads(conn.execute(
            "SELECT payload FROM task_events WHERE task_id=? AND kind='pr_acceptance'", (tid,)).fetchone()[0])
    # gh WAS invoked (the gate did not stop at profile resolution) with the operator's env.
    captured = json.loads(env_dump.read_text())
    assert captured["GH_TOKEN"] == "operator-token"
    assert receipt["login"] == "ambient (human lane)"
    assert "cannot be resolved" not in (receipt.get("detail") or "")
    # The (null-repository) refusal is attributed to the ambient login, not a profile.
    assert "the ambient gh login" in receipt["detail"]


def test_assignee_profile_home_lane_contract(tmp_path, monkeypatch):
    """human lane -> ambient (None); a real profile -> its home; an unknown profile -> auth."""
    from hermes_cli import kanban_pr_acceptance as acc

    launch_home = tmp_path / "home"
    (launch_home / "profiles" / "daedalus").mkdir(parents=True)
    (launch_home / "profiles" / "daedalus" / ".env").write_text("", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(launch_home))
    assert acc._assignee_profile_home("human:apollo") is None
    assert acc._assignee_profile_home("human") is None
    assert acc._assignee_profile_home(None) is None
    home = acc._assignee_profile_home("daedalus")
    assert home and os.path.realpath(home) == os.path.realpath(launch_home / "profiles" / "daedalus")
    with pytest.raises(acc._GateAuthError):
        acc._assignee_profile_home("ghost-profile")
    with pytest.raises(acc._GateAuthError):
        acc._assignee_profile_home("humane")  # prefix lookalike is a profile name, not the sentinel


# --- t_2315d06a: a pinned context delivered as a commit STATUS by the pinned app's bot ---
# The fleet lander posts `fleet/attribution` via the statuses API (never a check-run) while
# the ruleset pins it to the lander's integration id; GitHub accepts that status.

def _lander_status(login, type_="Bot", state="success", id_=7):
    return {"id": id_, "context": "required", "state": state,
            "target_url": None, "creator": {"login": login, "type": type_, "id": 333956532}}


@pytest.mark.platforms("posix")
@pytest.mark.parametrize("statuses, apps, expected", [
    # pinned (app 1) + status from app 1's own bot -> satisfied (red on main: 'missing')
    ([_lander_status("lander[bot]")], {"lander": 1}, "success"),
    # pinned + a failing status from the pinned bot is still a failure, not missing
    ([_lander_status("lander[bot]", state="failure")], {"lander": 1}, "failure"),
    # pinned + HUMAN-posted status (the spoof the pin exists for) -> missing
    ([_lander_status("mallory", type_="User")], {}, "missing"),
    # pinned + a different app's bot -> missing
    ([_lander_status("other[bot]")], {"other": 99}, "missing"),
    # a Bot-typed login whose slug does not resolve -> missing
    ([_lander_status("ghost[bot]")], {}, "missing"),
])
def test_pinned_context_status_only_from_the_pinned_apps_bot(github, statuses, apps, expected):
    from hermes_cli import kanban_pr_acceptance as acc
    github.update(conclusion="success", head="a" * 40, missing=True, statuses=statuses, apps=apps)
    receipt = acc.collect_acceptance("acme/repo", "https://github.com/acme/repo/pull/7")
    assert receipt["classification"] == expected, receipt
    assert receipt["ok"] is (expected == "success")
    if expected != "missing":
        assert receipt["checks"][0]["satisfied_by"] == f"status({statuses[0]['creator']['login']})"


@pytest.mark.platforms("posix")
def test_unpinned_context_still_accepts_any_status(github, monkeypatch):
    from hermes_cli import kanban_pr_acceptance as acc
    real = acc._api

    def unpinned(endpoint, **kw):
        value = real(endpoint, **kw)
        if endpoint.endswith("/branches/main"):
            value["protection"]["required_status_checks"]["checks"][0]["app_id"] = None
        return value
    monkeypatch.setattr(acc, "_api", unpinned)
    github.update(conclusion="success", head="a" * 40, missing=True,
                  statuses=[_lander_status("someone", type_="User")])
    receipt = acc.collect_acceptance("acme/repo", "https://github.com/acme/repo/pull/7")
    assert receipt["classification"] == "success", receipt
    # no app lookup is needed for an unpinned context
    assert not any(r.startswith("/apps/") for r in github["requests"])


# --- t_b5b407c8: required checks via REST, stage-named refusals, audited break-glass waiver ---
# Measured on t_f9dc8aad: the fleet App token is refused GraphQL ``branchProtectionRule``
# (HTTP 200 + FORBIDDEN), gh exits 1 without "HTTP 403", and the gate filed it as ``infra``.

_HEAD = "a" * 40


def _breakglass(tmp_path, *, head=_HEAD, rcs=None, tree=None, body_extra="", author="operator",
                path=None, contexts=("required",)):
    """A fleet-merge break-glass attribution comment + an off-box receipt under the hermes root."""
    receipt = path or (tmp_path / "home" / "var" / "ci-box" / "offbox-repo-7.receipt")
    receipt.parent.mkdir(parents=True, exist_ok=True)
    rc = {"ruff_rc": "0", "pytest_rc": "0", "semgrep_rc": "0", "gitleaks_rc": "0", **(rcs or {})}
    receipt.write_text(f"head={head}\ntree={tree or 'repo-7-' + head[:12]}\n"
                       + "".join(f"{k}={v}\n" for k, v in rc.items()), encoding="utf-8")
    absent = ",".join(f"{c} (required, ABSENT on head)" for c in contexts)
    body = (f"\U0001f916 merged-by: apollo \u00b7 lane: operator \u00b7 gate: ADVISORY \u00b7 why: x"
            f" \u00b7 offbox-security-receipt: {receipt} \u00b7 red-ci allowed [{absent}]: green receipt{body_extra}")
    return [{"id": 99, "user": {"login": author}, "body": body, "html_url": "https://github.com/acme/repo/pull/7#c99"}]


@pytest.mark.platforms("posix")
def test_app_token_without_admin_read_still_reads_required_checks(github):
    """RED on main (``infra``): required checks come from REST branch protection, which the
    App token can read; the GraphQL protection field it cannot read is no longer queried."""
    from hermes_cli import kanban_pr_acceptance as acc
    github.update(conclusion="success", head=_HEAD, app_token=True)
    receipt = acc.collect_acceptance("acme/repo", "https://github.com/acme/repo/pull/7")
    assert receipt["classification"] == "success", receipt
    assert receipt["required"] == [{"context": "required", "app_id": 1}]


@pytest.mark.platforms("posix")
def test_permission_refusal_is_auth_naming_the_stage_not_infra(github):
    from hermes_cli import kanban_pr_acceptance as acc
    github.update(conclusion="success", head=_HEAD, protection_denied=True)
    receipt = acc.collect_acceptance("acme/repo", "https://github.com/acme/repo/pull/7")
    assert receipt["classification"] == "auth", receipt
    assert receipt["stage"] == "read branch protection"
    assert "read branch protection" in receipt["detail"] and "gh:" not in receipt["detail"]


@pytest.mark.platforms("posix")
def test_infra_failure_names_stage_and_error_type(github, monkeypatch):
    from hermes_cli import kanban_pr_acceptance as acc
    real = acc._api

    def broken(endpoint, **kw):
        if "/check-runs" in endpoint:
            raise subprocess.TimeoutExpired("gh", 30)
        return real(endpoint, **kw)
    monkeypatch.setattr(acc, "_api", broken)
    github.update(conclusion="success", head=_HEAD)
    receipt = acc.collect_acceptance("acme/repo", "https://github.com/acme/repo/pull/7")
    assert receipt["classification"] == "infra"
    assert receipt["stage"] == "read check runs and statuses"
    assert receipt["error_type"] == "TimeoutExpired"


@pytest.mark.platforms("posix")
def test_transferred_repo_same_pr_is_accepted_and_recorded_without_retargeting(github):
    """The old owner's URL resolves (GitHub redirect) to the canonical repository; the
    receipt records it and the bound contract is left as written."""
    github.update(conclusion="success", head=_HEAD, canonical="newowner/repo")
    with connect() as conn:
        tid = kb.create_task(conn, title="moved", completion_contract="acme/repo")
        assert kb.complete_task(conn, tid, result="done", metadata={"published_pr": "https://github.com/acme/repo/pull/7"})
        receipt = json.loads(conn.execute(
            "SELECT payload FROM task_events WHERE task_id=? AND kind='pr_acceptance'", (tid,)).fetchone()[0])
        assert receipt["canonical_repo"] == "newowner/repo"
        assert kb.get_task(conn, tid).completion_contract == "https://github.com/acme/repo/pull/7"
        # A different PR is still refused against the bound contract.
        other = kb.create_task(conn, title="other", completion_contract="https://github.com/acme/repo/pull/7")
        assert not kb.complete_task(conn, other, result="done", metadata={"published_pr": "https://github.com/newowner/repo/pull/7"})


@pytest.mark.platforms("posix")
def test_merged_breakglass_with_green_receipt_waives_only_absent_checks(github, tmp_path):
    from hermes_cli import kanban_pr_acceptance as acc
    github.update(conclusion="success", head=_HEAD, missing=True, pr_state="MERGED",
                  comments=_breakglass(tmp_path))
    receipt = acc.collect_acceptance("acme/repo", "https://github.com/acme/repo/pull/7")
    assert receipt["classification"] == "success", receipt
    assert receipt["checks"][0]["classification"] == "waived"
    waiver = receipt["waiver"]
    assert waiver["kind"] == "break-glass-offbox" and waiver["merged_by"] == "operator"
    assert waiver["contexts"] == ["required"] and len(waiver["receipt_sha256"]) == 64


@pytest.mark.platforms("posix")
@pytest.mark.parametrize("case", ["open_pr", "wrong_head", "red_rung", "wrong_tree", "not_merger",
                                  "outside_root", "context_not_listed", "failed_check", "no_comment"])
def test_breakglass_waiver_negatives_stay_closed(github, tmp_path, case):
    from hermes_cli import kanban_pr_acceptance as acc
    kw = {}
    state = {"conclusion": "success", "head": _HEAD, "missing": True, "pr_state": "MERGED"}
    if case == "open_pr":
        state["pr_state"] = "OPEN"
    elif case == "wrong_head":
        kw["head"] = "b" * 40
    elif case == "red_rung":
        kw["rcs"] = {"pytest_rc": "1"}
    elif case == "wrong_tree":
        kw["tree"] = "other-7-" + _HEAD[:12]
    elif case == "not_merger":
        kw["author"] = "mallory"
    elif case == "outside_root":
        kw["path"] = tmp_path / "elsewhere" / "offbox.receipt"
    elif case == "context_not_listed":
        kw["contexts"] = ("sast",)
    elif case == "failed_check":
        state.update(missing=False, conclusion="failure")
    github.update(state, comments=[] if case == "no_comment" else _breakglass(tmp_path, **kw))
    receipt = acc.collect_acceptance("acme/repo", "https://github.com/acme/repo/pull/7")
    assert receipt["ok"] is False and "waiver" not in receipt, receipt
    assert receipt["classification"] == ("failure" if case == "failed_check" else "missing")


@pytest.mark.platforms("posix")
def test_tampered_receipt_after_comment_is_not_trusted(github, tmp_path):
    from hermes_cli import kanban_pr_acceptance as acc
    comments = _breakglass(tmp_path)
    receipt_path = tmp_path / "home" / "var" / "ci-box" / "offbox-repo-7.receipt"
    receipt_path.write_text(receipt_path.read_text().replace("pytest_rc=0", "pytest_rc=1"))
    github.update(conclusion="success", head=_HEAD, missing=True, pr_state="MERGED", comments=comments)
    assert acc.collect_acceptance("acme/repo", "https://github.com/acme/repo/pull/7")["ok"] is False


@pytest.mark.platforms("posix")
def test_cli_names_the_acceptance_refusal_not_a_concurrency_guess(github, tmp_path):
    """The real ``hermes kanban complete`` subprocess prints the gate's classification/stage.
    RED on main: "refused while status is 'ready' (state changed concurrently?)"."""
    github.update(conclusion="success", head=_HEAD, protection_denied=True)
    with connect() as conn:
        tid = kb.create_task(conn, title="cli", completion_contract="https://github.com/acme/repo/pull/7")
    env = dict(os.environ, PYTHONPATH=str(Path(__file__).parents[2]))
    for name in ("HERMES_KANBAN_TASK", "HERMES_KANBAN_RUN_ID", "HERMES_KANBAN_OWNER_PID"):
        env.pop(name, None)
    proc = subprocess.run([sys.executable, "-m", "hermes_cli.main", "kanban", "complete", tid,
                           "--summary", "done", "--metadata",
                           json.dumps({"published_pr": "https://github.com/acme/repo/pull/7"})],
                          env=env, cwd=Path(__file__).parents[2], capture_output=True, text=True, timeout=120)
    assert proc.returncode != 0
    assert "PR acceptance refused (auth at stage 'read branch protection')" in proc.stderr, proc.stderr
    assert "changed concurrently" not in proc.stderr
