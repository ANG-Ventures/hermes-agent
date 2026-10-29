"""Policy gates for explicit flagship model overrides."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from hermes_cli import kanban as kc
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli.model_policy import firepower_guard_error, route_kind


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _created_id(output: str) -> str:
    match = re.search(r"(t_[a-f0-9]+)", output)
    assert match, output
    return match.group(1)


@pytest.mark.parametrize("model", [
    "gpt-6-astra-900k",
    "openai-codex/gpt-6-astra-900k",
    "claude-fable-5-1",
])
def test_guard_requires_nonempty_reason_for_flagship(model):
    assert "--firepower" in firepower_guard_error(model, None)
    assert "--firepower" in firepower_guard_error(model, "  ")
    assert firepower_guard_error(model, "hard architecture adjudication") is None


def test_guard_does_not_restrict_sub_flagship():
    assert firepower_guard_error("gpt-5.6-sol-900k", None) is None
    assert firepower_guard_error("claude-opus-5", None) is None
    assert route_kind("openai-codex/gpt-5.6-sol-900k") == "standard"
    assert route_kind("openai-codex/gpt-6-astra-900k") == "firepower"


def test_create_refuses_flagship_without_firepower_reason(kanban_home):
    output = kc.run_slash(
        "create 'expensive task' --assignee worker "
        "--model gpt-6-astra-900k --provider openai-codex"
    )
    # create is main's #823 path: its message names --allow-flagship
    # (--firepower is an argparse alias for the same dest).
    assert "--allow-flagship" in output
    with kb.connect() as conn:
        count = conn.execute("SELECT count(*) FROM tasks").fetchone()[0]
    assert count == 0


def test_create_accepts_flagship_and_appends_audit_comment(kanban_home):
    reason = "cross-module concurrency diagnosis"
    output = kc.run_slash(
        "create 'expensive task' --assignee worker "
        "--model gpt-6-astra-900k --provider openai-codex "
        f"--firepower '{reason}'"
    )
    task_id = _created_id(output)
    with kb.connect() as conn:
        task = kb.get_task(conn, task_id)
        comments = kb.list_comments(conn, task_id)
    assert task.model_override == "gpt-6-astra-900k"
    assert len(comments) == 1
    assert reason in comments[0].body
    # The dispatcher's flagship gate authorizes on exactly this prefix.
    assert comments[0].body.startswith("flagship override:")


def test_create_rolls_back_if_firepower_audit_comment_fails(
    kanban_home, monkeypatch
):
    def fail_comment(*_args, **_kwargs):
        raise RuntimeError("audit unavailable")

    monkeypatch.setattr(kb, "add_comment", fail_comment)
    output = kc.run_slash(
        "create 'must stay audited' --assignee worker "
        "--model gpt-6-astra-900k --provider openai-codex "
        "--firepower 'hard recovery'"
    )
    assert "audit unavailable" in output
    with kb.connect() as conn:
        assert conn.execute("SELECT count(*) FROM tasks").fetchone()[0] == 0


def test_set_model_refuses_before_mutating_existing_pin(kanban_home):
    output = kc.run_slash("create 'plain task' --assignee worker --model gpt-5.6-sol-900k")
    task_id = _created_id(output)

    rejected = kc.run_slash(
        f"set-model {task_id} gpt-6-astra-900k --provider openai-codex"
    )

    assert "--firepower" in rejected
    with kb.connect() as conn:
        task = kb.get_task(conn, task_id)
        comments = kb.list_comments(conn, task_id)
    assert task.model_override == "gpt-5.6-sol-900k"
    assert comments == []


def test_set_model_accepts_flagship_and_appends_audit_comment(kanban_home):
    output = kc.run_slash("create 'plain task' --assignee worker")
    task_id = _created_id(output)
    reason = "ambiguous multi-file recovery"

    changed = kc.run_slash(
        f"set-model {task_id} gpt-6-astra-900k --provider openai-codex "
        f"--firepower '{reason}'"
    )

    assert "Set model override" in changed
    with kb.connect() as conn:
        task = kb.get_task(conn, task_id)
        comments = kb.list_comments(conn, task_id)
    assert task.model_override == "gpt-6-astra-900k"
    assert len(comments) == 1
    assert reason in comments[0].body


def test_set_model_rolls_back_if_firepower_audit_comment_fails(
    kanban_home, monkeypatch
):
    with kb.connect() as conn:
        task_id = kb.create_task(
            conn,
            title="atomic override",
            assignee="worker",
            model_override="gpt-5.6-sol-900k",
        )

    def fail_comment(*_args, **_kwargs):
        raise RuntimeError("audit unavailable")

    monkeypatch.setattr(kb, "add_comment", fail_comment)
    with kb.connect() as conn:
        with pytest.raises(RuntimeError, match="audit unavailable"):
            kb.set_model_override(
                conn,
                task_id,
                "gpt-6-astra-900k",
                provider="openai-codex",
                audit_comment_author="operator",
                audit_comment_body="flagship override: reason=hard recovery",
            )
        task = kb.get_task(conn, task_id)
    assert task is not None
    assert task.model_override == "gpt-5.6-sol-900k"


def test_db_layer_refuses_flagship_route_without_dispatch_authorizing_comment(
    kanban_home,
):
    """A route the DB writes must never be one the dispatcher then refuses.

    main's dispatcher gate (``flagship_refused``) only accepts a
    ``flagship override:`` comment, so the batch/lane writer requires exactly
    that prefix rather than any free-text audit body.
    """
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="t", assignee="worker")
        with pytest.raises(ValueError, match="orchestrator-only"):
            kb.apply_batch_route_writes(conn, [kb.BatchRouteWrite(
                task_id=task_id, touch_model=True, model="gpt-6-astra-900k",
                provider="openai-codex", audit_comment_author="op",
                audit_comment_body="some other note",
            )])
        with pytest.raises(ValueError, match="orchestrator-only"):
            kb.set_model_override(conn, task_id, "gpt-6-astra-900k")
        assert kb.get_task(conn, task_id).model_override is None
        assert kb.list_comments(conn, task_id) == []


def test_batch_flagship_route_passes_main_dispatch_gate(kanban_home):
    """set-model --where with --firepower yields a card main's gate admits."""
    with kb.connect() as conn:
        ids = [kb.create_task(conn, title=f"c{i}", assignee="worker") for i in range(2)]
    out = kc.run_slash(
        "set-model gpt-6-astra-900k --where assignee=worker "
        "--firepower 'capacity incident'"
    )
    for task_id in ids:
        assert f"{task_id}: route=gpt-6-astra-900k" in out, out
    with kb.connect() as conn:
        res = kbd.dispatch_once(conn, dry_run=True)
        for task_id in ids:
            bodies = [c.body for c in kb.list_comments(conn, task_id)]
            assert any(b.startswith("flagship override:") for b in bodies), bodies
    assert not (set(res.flagship_refused) & set(ids)), res.flagship_refused


def test_effective_worker_route_reads_profile_default(kanban_home):
    profile = kanban_home / "profiles" / "worker"
    profile.mkdir(parents=True)
    (profile / "config.yaml").write_text(
        "model:\n  default: openai-codex/gpt-5.6-sol-900k\n  provider: openai-codex\n",
        encoding="utf-8",
    )
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="plain", assignee="worker")
        task = kb.get_task(conn, task_id)
    assert kb.effective_worker_route(task) == "openai-codex/gpt-5.6-sol-900k"


def test_effective_worker_route_prefers_card_override(kanban_home):
    with kb.connect() as conn:
        task_id = kb.create_task(
            conn,
            title="pinned",
            assignee="worker",
            model_override="gpt-5.6-sol-900k",
            provider_override="openai-codex",
        )
        task = kb.get_task(conn, task_id)
    assert kb.effective_worker_route(task) == "openai-codex/gpt-5.6-sol-900k"


def test_dispatch_tick_records_route_for_each_spawn(kanban_home, monkeypatch):
    from hermes_cli import profiles

    profile = kanban_home / "profiles" / "worker"
    profile.mkdir(parents=True)
    (profile / "config.yaml").write_text(
        "model:\n  default: openai-codex/gpt-5.6-sol-900k\n  provider: openai-codex\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(profiles, "profile_exists", lambda _name: True)
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="route announce", assignee="worker")
        result = kbd.dispatch_once(
            conn,
            spawn_fn=lambda _task, _workspace: 12345,
            reconcile_orphans=False,
        )
    assert result.spawn_routes == {
        task_id: "openai-codex/gpt-5.6-sol-900k",
    }


# --- assignee profile default route (t_9e7ba884) ---------------------------
# The assignee IS a route choice: a profile whose model.default is a flagship
# must need the same --allow-flagship reason as an explicit --model.


def _profile(kanban_home, name: str, model: str) -> None:
    profile = kanban_home / "profiles" / name
    profile.mkdir(parents=True, exist_ok=True)
    (profile / "config.yaml").write_text(
        f"model:\n  default: {model}\n  provider: claude-apr\n", encoding="utf-8",
    )


@pytest.fixture
def flagship_profiles(kanban_home):
    _profile(kanban_home, "daedalus-fable", "claude-fable-5-1")
    _profile(kanban_home, "daedalus-astra", "gpt-6-astra-900k")
    _profile(kanban_home, "daedalus-opus", "claude-opus-5-5")
    _profile(kanban_home, "daedalus-sonnet", "claude-sonnet-5")
    _profile(kanban_home, "daedalus-luna", "gpt-5.6-luna")
    return kanban_home


@pytest.mark.parametrize("assignee", ["daedalus-fable", "daedalus-astra"])
def test_create_refuses_flagship_profile_assignee_without_reason(
    flagship_profiles, assignee,
):
    output = kc.run_slash(f"create 'GATE-PROBE' --assignee {assignee}")
    assert "--allow-flagship" in output, output
    assert "assignee profile default" in output, output
    with kb.connect() as conn:
        assert conn.execute("SELECT count(*) FROM tasks").fetchone()[0] == 0


def test_create_accepts_flagship_profile_assignee_with_reason_and_audits(
    flagship_profiles,
):
    reason = "delicate infra: dispatcher lock rewrite"
    output = kc.run_slash(
        f"create 'GATE-PROBE' --assignee daedalus-fable --allow-flagship '{reason}'"
    )
    task_id = _created_id(output)
    with kb.connect() as conn:
        bodies = [c.body for c in kb.list_comments(conn, task_id)]
        res = kbd.dispatch_once(conn, dry_run=True)
    assert bodies == [f"flagship override: {reason}"], bodies
    assert task_id not in res.flagship_refused


@pytest.mark.parametrize(
    "assignee", ["daedalus-opus", "daedalus-sonnet", "daedalus-luna", "worker"],
)
def test_non_flagship_profile_assignees_unaffected(flagship_profiles, assignee):
    output = kc.run_slash(f"create 'plain' --assignee {assignee}")
    task_id = _created_id(output)
    with kb.connect() as conn:
        assert kb.list_comments(conn, task_id) == []
        res = kbd.dispatch_once(conn, dry_run=True)
        other = kb.create_task(conn, title="o", assignee="daedalus-fable",
                               flagship_override_reason="seed")
    assert task_id not in res.flagship_refused
    assert kc.run_slash(f"reassign {other} {assignee}").startswith("Reassigned")


def test_card_pin_on_flagship_profile_is_the_route(flagship_profiles):
    """A non-flagship --model pin replaces the profile default: no reason needed."""
    output = kc.run_slash(
        "create 'pinned' --assignee daedalus-fable --model claude-opus-5-5"
    )
    task_id = _created_id(output)
    with kb.connect() as conn:
        assert kb.get_task(conn, task_id).model_override == "claude-opus-5-5"


@pytest.mark.parametrize("verb", ["reassign", "assign"])
def test_reassign_to_flagship_profile_refuses_without_reason(flagship_profiles, verb):
    task_id = _created_id(kc.run_slash("create 'plain' --assignee daedalus-opus"))
    output = kc.run_slash(f"{verb} {task_id} daedalus-fable")
    assert "assignee profile default" in output, output
    assert "--allow-flagship" in output, output
    with kb.connect() as conn:
        assert kb.get_task(conn, task_id).assignee == "daedalus-opus"
        assert kb.list_comments(conn, task_id) == []


@pytest.mark.parametrize("verb", ["reassign", "assign"])
def test_reassign_to_flagship_profile_with_reason_audits(flagship_profiles, verb):
    task_id = _created_id(kc.run_slash("create 'plain' --assignee daedalus-opus"))
    output = kc.run_slash(
        f"{verb} {task_id} daedalus-fable --allow-flagship 'lock rewrite'"
    )
    assert "daedalus-fable" in output, output
    with kb.connect() as conn:
        assert kb.get_task(conn, task_id).assignee == "daedalus-fable"
        bodies = [c.body for c in kb.list_comments(conn, task_id)]
        res = kbd.dispatch_once(conn, dry_run=True)
    assert bodies == ["flagship override: lock rewrite"], bodies
    assert task_id not in res.flagship_refused


def test_dispatch_refuses_flagship_profile_default_without_override(
    flagship_profiles,
):
    """A card that reached ready on a flagship profile by another path (an
    older card, a raw row write) is refused at dispatch, same comment prefix."""
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="legacy", assignee="daedalus-opus")
        conn.execute("UPDATE tasks SET assignee = 'daedalus-fable' WHERE id = ?",
                     (task_id,))
        conn.commit()
        res = kbd.dispatch_once(conn, spawn_fn=lambda _t, _w: 12345,
                                reconcile_orphans=False)
        bodies = [c.body for c in kb.list_comments(conn, task_id)]
        task = kb.get_task(conn, task_id)
    assert task_id in res.flagship_refused
    assert task.status == "ready"
    assert any(
        b.startswith("flagship dispatch refused:") and "assignee profile default" in b
        and "claude-fable-5-1" in b
        for b in bodies
    ), bodies


@pytest.fixture
def proven_operator(monkeypatch):
    """The CLI caller is a PROVEN operator (Apollo's gateway tree, or the
    operator token). Unproven, its label is ``unverified:default`` and its
    override comment does not pre-authorize later verbs (Prism r7 bf4b5b6f26f8)."""
    from hermes_cli import kanban_identity

    monkeypatch.setattr(kanban_identity, "operator_label_proven", lambda _n: True)


def test_assign_same_flagship_profile_with_reason_authorizes_legacy_card(
    flagship_profiles, proven_operator,
):
    """Prism 70a0613a6b24: re-assigning a legacy flagship card to its own
    profile with --allow-flagship must record the override, not drop it."""
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="legacy", assignee="daedalus-opus")
        conn.execute("UPDATE tasks SET assignee = 'daedalus-fable' WHERE id = ?",
                     (task_id,))
        conn.commit()
    out = kc.run_slash(f"assign {task_id} daedalus-fable --allow-flagship 'approved'")
    assert "Assigned" in out, out
    with kb.connect() as conn:
        bodies = [c.body for c in kb.list_comments(conn, task_id)]
        res = kbd.dispatch_once(conn, dry_run=True)
    assert bodies == ["flagship override: approved"], bodies
    assert task_id not in res.flagship_refused
    # A second identical call does not stack duplicate overrides.
    kc.run_slash(f"assign {task_id} daedalus-fable --allow-flagship 'approved'")
    with kb.connect() as conn:
        assert len(kb.list_comments(conn, task_id)) == 1


# ---- Prism r2: the gate judges the model the worker will RUN ----------------
# (22663f69a1d3: profile aliases; 9e7596b4c0ed: ${VAR} expansion + managed overlay)

def _raw_profile(kanban_home, name: str, body: str) -> None:
    profile = kanban_home / "profiles" / name
    profile.mkdir(parents=True, exist_ok=True)
    (profile / "config.yaml").write_text(body, encoding="utf-8")


def _assert_gated(assignee: str) -> None:
    output = kc.run_slash(f"create 'GATE-PROBE' --assignee {assignee}")
    assert "assignee profile default" in output, output
    assert "claude-fable-5-1" in output, output
    with kb.connect() as conn:
        # Seed unassigned: under a managed pin every profile route is gated.
        task_id = kb.create_task(conn, title="legacy", assignee=None)
        conn.execute("UPDATE tasks SET assignee = ? WHERE id = ?", (assignee, task_id))
        conn.commit()
        res = kbd.dispatch_once(conn, dry_run=True)
    assert task_id in res.flagship_refused


@pytest.mark.parametrize("aliases", [
    "  aliases:\n    fast: claude-apr/claude-fable-5-1\n",
    "  aliases:\n    fast: {model: claude-fable-5-1, provider: claude-apr}\n",
    None,  # top-level model_aliases form
])
def test_profile_alias_resolving_to_flagship_is_gated(flagship_profiles, aliases):
    body = "model:\n  default: fast\n  provider: claude-apr\n"
    if aliases is None:
        body += "model_aliases:\n  fast: {model: claude-fable-5-1, provider: claude-apr}\n"
    else:
        body += aliases
    _raw_profile(flagship_profiles, "worker-fast", body)
    _assert_gated("worker-fast")


def test_profile_alias_named_like_flagship_but_resolving_to_opus_is_not_gated(
    flagship_profiles,
):
    _raw_profile(flagship_profiles, "worker-lite",
                 "model:\n  default: fable-lite\n  provider: claude-apr\n"
                 "  aliases:\n    fable-lite: claude-apr/claude-opus-5-5\n")
    task_id = _created_id(kc.run_slash("create 'plain' --assignee worker-lite"))
    with kb.connect() as conn:
        assert kb.list_comments(conn, task_id) == []
        assert task_id not in kbd.dispatch_once(conn, dry_run=True).flagship_refused


def test_profile_env_var_default_is_expanded_before_the_gate(
    flagship_profiles, monkeypatch,
):
    # The var lives in the assignee's own .env (its secret scope), not the
    # caller's env: Prism r3 858c7319e28f.
    monkeypatch.delenv("T9E_WORKER_MODEL", raising=False)
    _raw_profile(flagship_profiles, "worker-env",
                 "model:\n  default: ${T9E_WORKER_MODEL}\n  provider: claude-apr\n")
    (flagship_profiles / "profiles" / "worker-env" / ".env").write_text(
        "T9E_WORKER_MODEL=claude-fable-5-1\n", encoding="utf-8")
    _assert_gated("worker-env")


def test_managed_overlay_default_is_the_gated_route(
    flagship_profiles, monkeypatch, tmp_path,
):
    from hermes_cli import managed_scope

    managed = tmp_path / "managed"
    managed.mkdir()
    (managed / "config.yaml").write_text("model:\n  default: claude-fable-5-1\n",
                                         encoding="utf-8")
    monkeypatch.setenv("HERMES_MANAGED_DIR", str(managed))
    managed_scope.invalidate_managed_cache()
    try:
        # daedalus-opus's own file says claude-opus-5-5; the managed pin wins.
        _assert_gated("daedalus-opus")
    finally:
        monkeypatch.delenv("HERMES_MANAGED_DIR")
        managed_scope.invalidate_managed_cache()


# ---- Prism r3 -----------------------------------------------------------------
# 858c7319e28f / 13a34c96f1e2: ${VAR} resolves in the ASSIGNEE's secret scope.
# 311c94f92990: the create gate judges the FINAL (post-remap) assignee.

def _profile_env(kanban_home, name: str, text: str) -> None:
    (kanban_home / "profiles" / name / ".env").write_text(text, encoding="utf-8")


def test_profile_dotenv_var_beats_caller_env_and_is_gated(flagship_profiles, monkeypatch):
    monkeypatch.setenv("T9E_WORKER_MODEL", "claude-opus-5-5")  # the caller's value
    _raw_profile(flagship_profiles, "worker-env",
                 "model:\n  default: ${T9E_WORKER_MODEL}\n  provider: claude-apr\n")
    _profile_env(flagship_profiles, "worker-env", "T9E_WORKER_MODEL=claude-fable-5-1\n")
    _assert_gated("worker-env")


def test_profile_dotenv_non_flagship_var_is_not_gated_by_caller_env(
    flagship_profiles, monkeypatch,
):
    monkeypatch.setenv("T9E_WORKER_MODEL", "claude-fable-5-1")  # the caller's value
    _raw_profile(flagship_profiles, "worker-env",
                 "model:\n  default: ${T9E_WORKER_MODEL}\n  provider: claude-apr\n")
    _profile_env(flagship_profiles, "worker-env", "T9E_WORKER_MODEL=claude-opus-5-5\n")
    task_id = _created_id(kc.run_slash("create 'plain' --assignee worker-env"))
    with kb.connect() as conn:
        assert kb.list_comments(conn, task_id) == []
        assert task_id not in kbd.dispatch_once(conn, dry_run=True).flagship_refused


@pytest.fixture
def worker_remap(flagship_profiles, monkeypatch):
    """A dispatched worker minting a child under a parent; placeholder
    assignees remap to ``kanban.default_assignee``."""
    from hermes_cli import kanban_worker_policy as kwp

    with kb.connect() as conn:
        parent = kb.create_task(conn, title="parent", assignee="daedalus-opus")
    monkeypatch.setenv("HERMES_KANBAN_TASK", parent)

    def _set_default(name):
        monkeypatch.setattr(kwp, "_load_config",
                            lambda: {"kanban": {"default_assignee": name}})
    return parent, _set_default


def test_placeholder_remapped_to_flagship_profile_with_worker_reason_refuses(worker_remap):
    # r3 kept a worker's reason here; r6 (423541efe928) makes a worker's reason
    # itself the refusal: flagship authority is orchestrator-only.
    parent, set_default = worker_remap
    set_default("daedalus-fable")
    with kb.connect() as conn:
        before = conn.execute("SELECT count(*) FROM tasks").fetchone()[0]
        with pytest.raises(ValueError, match="operator/orchestrator-only"):
            kb.create_task(conn, title="child", assignee="human", parents=[parent],
                           flagship_override_reason="approved infra")
        assert conn.execute("SELECT count(*) FROM tasks").fetchone()[0] == before


def test_placeholder_remapped_to_flagship_profile_without_reason_refuses(worker_remap):
    parent, set_default = worker_remap
    set_default("daedalus-fable")
    with kb.connect() as conn:
        before = conn.execute("SELECT count(*) FROM tasks").fetchone()[0]
        with pytest.raises(ValueError, match="assignee profile default of 'daedalus-fable'"):
            kb.create_task(conn, title="child", assignee="human", parents=[parent])
        assert conn.execute("SELECT count(*) FROM tasks").fetchone()[0] == before


def test_flagship_placeholder_remapped_to_cheap_profile_is_accepted(
    flagship_profiles, worker_remap,
):
    parent, set_default = worker_remap
    _profile(flagship_profiles, "apollo", "claude-fable-5-1")
    set_default("daedalus-opus")
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="child", assignee="apollo", parents=[parent])
        assert kb.get_task(conn, task_id).assignee == "daedalus-opus"
        assert kb.list_comments(conn, task_id) == []


# ---- Prism r5 -----------------------------------------------------------------
# d1a3754ac7e5: the gate judges DISPATCHABLE routes; a swarm's no-worker root is not.
# f6a54f1196fd: a pin clear is judged on the RESULTING (profile) route, before reclaim.
# 69a5d42a9476: an unresolved ${VAR} default fails closed.

def test_swarm_root_for_flagship_default_caller_is_not_gated(flagship_profiles):
    _profile(flagship_profiles, "apollo", "claude-fable-5-1")
    with kb.connect() as conn:
        from hermes_cli import kanban_swarm as ks

        created = ks.create_swarm(
            conn, goal="probe", verifier_assignee="daedalus-opus",
            synthesizer_assignee="daedalus-opus", created_by="apollo",
            workers=[ks.SwarmWorkerSpec(profile="daedalus-opus", title="w1", body="")],
        )
        root = kb.get_task(conn, created.root_id)
        assert root.assignee == "apollo"
        assert root.status == "done"
        assert not any(
            c.body.startswith("flagship override:")
            for c in kb.list_comments(conn, created.root_id)
        )
        res = kbd.dispatch_once(conn, dry_run=True)
    assert not set(res.flagship_refused) & {created.root_id, *created.worker_ids}


def test_swarm_flagship_child_without_reason_is_refused(flagship_profiles):
    output = kc.run_slash(
        "swarm 'probe' --worker 'daedalus-fable:w1' --verifier daedalus-opus "
        "--synthesizer daedalus-opus"
    )
    assert "assignee profile default of 'daedalus-fable'" in output, output
    with kb.connect() as conn:
        assert conn.execute("SELECT count(*) FROM tasks").fetchone()[0] == 0


def test_swarm_allow_flagship_reason_reaches_the_children(flagship_profiles):
    output = kc.run_slash(
        "swarm 'probe' --worker 'daedalus-fable:w1' --verifier daedalus-opus "
        "--synthesizer daedalus-opus --allow-flagship 'approved infra' --json"
    )
    import json as _json

    created = _json.loads(output)
    worker = created["worker_ids"][0]
    with kb.connect() as conn:
        bodies = [c.body for c in kb.list_comments(conn, worker)]
        verifier_bodies = [c.body for c in kb.list_comments(conn, created["verifier_id"])]
    assert "flagship override: approved infra" in bodies, bodies
    # A non-flagship child gets no override comment from the shared reason.
    assert not any(b.startswith("flagship override:") for b in verifier_bodies)


def _pinned_flagship_card(conn) -> str:
    task_id = kb.create_task(conn, title="pinned", assignee="daedalus-fable",
                             model_override="claude-opus-5-5")
    assert kb.list_comments(conn, task_id) == []
    return task_id


def test_set_model_clear_onto_flagship_default_without_reason_refuses(
    flagship_profiles, monkeypatch,
):
    reclaimed = []
    monkeypatch.setattr(kb, "reclaim_task", lambda *a, **k: reclaimed.append(a) or True)
    with kb.connect() as conn:
        task_id = _pinned_flagship_card(conn)
    output = kc.run_slash(f"set-model {task_id} --reclaim")
    assert "assignee profile default of 'daedalus-fable'" in output, output
    assert reclaimed == []
    with kb.connect() as conn:
        assert kb.get_task(conn, task_id).model_override == "claude-opus-5-5"
        assert kb.list_comments(conn, task_id) == []


def test_set_model_clear_onto_flagship_default_with_reason_authorizes(flagship_profiles):
    with kb.connect() as conn:
        task_id = _pinned_flagship_card(conn)
    output = kc.run_slash(f"set-model {task_id} --allow-flagship 'approved infra'")
    with kb.connect() as conn:
        task = kb.get_task(conn, task_id)
        bodies = [c.body for c in kb.list_comments(conn, task_id)]
        res = kbd.dispatch_once(conn, dry_run=True)
    assert task.model_override is None, output
    assert bodies == ["flagship override: approved infra"], bodies
    assert task_id not in res.flagship_refused


def test_db_set_model_clear_onto_flagship_default(flagship_profiles):
    with kb.connect() as conn:
        task_id = _pinned_flagship_card(conn)
        with pytest.raises(ValueError, match="assignee profile default"):
            kb.set_model_override(conn, task_id, None)
        assert kb.get_task(conn, task_id).model_override == "claude-opus-5-5"
        assert kb.set_model_override(conn, task_id, None,
                                     flagship_override_reason="approved")
        bodies = [c.body for c in kb.list_comments(conn, task_id)]
        assert bodies == ["flagship override: approved"], bodies
        assert task_id not in kbd.dispatch_once(conn, dry_run=True).flagship_refused


def test_set_model_clear_onto_cheap_default_needs_no_reason(flagship_profiles):
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="p", assignee="daedalus-opus",
                                 model_override="claude-sonnet-5")
        assert kb.set_model_override(conn, task_id, None)
        assert kb.list_comments(conn, task_id) == []


def test_unresolved_env_default_fails_closed(flagship_profiles, monkeypatch):
    monkeypatch.delenv("T9E_UNSET_MODEL", raising=False)
    _raw_profile(flagship_profiles, "worker-unres",
                 "model:\n  default: ${T9E_UNSET_MODEL}\n  provider: claude-apr\n")
    output = kc.run_slash("create 'GATE-PROBE' --assignee worker-unres")
    assert "cannot resolve worker-unres model default" in output, output
    assert "--allow-flagship" in output, output
    with kb.connect() as conn:
        assert conn.execute("SELECT count(*) FROM tasks").fetchone()[0] == 0
        task_id = kb.create_task(conn, title="legacy", assignee=None)
        conn.execute("UPDATE tasks SET assignee = 'worker-unres' WHERE id = ?", (task_id,))
        conn.commit()
        assert task_id in kbd.dispatch_once(conn, dry_run=True).flagship_refused
    ok = _created_id(kc.run_slash(
        "create 'x' --assignee worker-unres --allow-flagship 'known lane'"))
    with kb.connect() as conn:
        assert [c.body for c in kb.list_comments(conn, ok)] == ["flagship override: known lane"]


# ---- Prism r6 -----------------------------------------------------------------
# 423541efe928: a dispatched worker's --allow-flagship is refused at the DB layer,
#               so the CLI and a direct kb call get the tool's answer.
# 7018f089fcce: an empty model.default adopts the named provider's default_model.

@pytest.fixture
def worker_env(flagship_profiles, monkeypatch):
    with kb.connect() as conn:
        parent = kb.create_task(conn, title="parent", assignee="daedalus-opus")
        target = kb.create_task(conn, title="target", assignee="daedalus-opus")
        pinned = kb.create_task(conn, title="pinned", assignee="daedalus-opus",
                                model_override="claude-opus-5-5")
    monkeypatch.setenv("HERMES_KANBAN_TASK", parent)
    return parent, target, pinned


def _card_count() -> int:
    with kb.connect() as conn:
        return conn.execute("SELECT count(*) FROM tasks").fetchone()[0]


def test_worker_cli_create_allow_flagship_is_refused(worker_env):
    parent, _target, _pinned = worker_env
    before = _card_count()
    out = kc.run_slash(f"create 'child' --assignee daedalus-fable --parent {parent} "
                       "--allow-flagship 'self approved'")
    assert "operator/orchestrator-only" in out, out
    assert _card_count() == before
    out = kc.run_slash(f"create 'child' --model claude-fable-5-1 --parent {parent} "
                       "--allow-flagship 'self approved'")
    assert "operator/orchestrator-only" in out, out
    assert _card_count() == before


def test_worker_db_create_task_with_reason_is_refused(worker_env):
    parent, _target, _pinned = worker_env
    before = _card_count()
    with kb.connect() as conn:
        with pytest.raises(ValueError, match="operator/orchestrator-only"):
            kb.create_task(conn, title="c", assignee="daedalus-fable", parents=[parent],
                           flagship_override_reason="self approved")
    assert _card_count() == before


@pytest.mark.parametrize("verb", ["reassign", "assign"])
def test_worker_cli_assign_allow_flagship_is_refused(worker_env, verb):
    _parent, target, _pinned = worker_env
    out = kc.run_slash(f"{verb} {target} daedalus-fable --allow-flagship 'self approved'")
    assert "operator/orchestrator-only" in out, out
    with kb.connect() as conn:
        assert kb.get_task(conn, target).assignee == "daedalus-opus"
        assert kb.list_comments(conn, target) == []
        with pytest.raises(kb.FlagshipAssigneeRefused, match="operator/orchestrator-only"):
            kb.reassign_task(conn, target, "daedalus-fable",
                             flagship_override_reason="self approved")
        assert kb.get_task(conn, target).assignee == "daedalus-opus"


def test_worker_set_model_allow_flagship_is_refused(worker_env):
    _parent, _target, pinned = worker_env
    out = kc.run_slash(f"set-model {pinned} claude-fable-5-1 --allow-flagship 'self approved'")
    assert "operator/orchestrator-only" in out, out
    with kb.connect() as conn:
        assert kb.get_task(conn, pinned).model_override == "claude-opus-5-5"
        with pytest.raises(ValueError, match="operator/orchestrator-only"):
            kb.set_model_override(conn, pinned, "claude-fable-5-1",
                                  flagship_override_reason="self approved")
        assert kb.get_task(conn, pinned).model_override == "claude-opus-5-5"
        assert kb.list_comments(conn, pinned) == []


def test_worker_verbs_without_a_reason_are_unchanged(worker_env):
    parent, target, pinned = worker_env
    out = kc.run_slash(f"create 'child' --assignee daedalus-opus --parent {parent}")
    assert _created_id(out)
    assert kc.run_slash(f"reassign {target} daedalus-sonnet").startswith("Reassigned")
    with kb.connect() as conn:
        assert kb.set_model_override(conn, pinned, "claude-sonnet-5")
        assert kb.get_task(conn, pinned).model_override == "claude-sonnet-5"


_FLEET_PROVIDER = (
    "model:\n  provider: fleet\n"
    "providers:\n  fleet:\n    base_url: http://127.0.0.1:9/v1\n"
    "    default_model: {model}\n"
)


@pytest.mark.parametrize("body", [
    _FLEET_PROVIDER.format(model="claude-fable-5-1"),
    "model:\n  provider: fleet\ncustom_providers:\n"
    "  - name: fleet\n    base_url: http://127.0.0.1:9/v1\n    model: claude-fable-5-1\n",
])
def test_provider_default_model_is_the_gated_route(flagship_profiles, body):
    _raw_profile(flagship_profiles, "worker-prov", body)
    _assert_gated("worker-prov")


def test_provider_default_model_non_flagship_is_not_gated(flagship_profiles):
    _raw_profile(flagship_profiles, "worker-prov",
                 _FLEET_PROVIDER.format(model="claude-opus-5-5"))
    task_id = _created_id(kc.run_slash("create 'plain' --assignee worker-prov"))
    with kb.connect() as conn:
        assert kb.list_comments(conn, task_id) == []
        assert task_id not in kbd.dispatch_once(conn, dry_run=True).flagship_refused


def test_provider_without_a_findable_default_fails_closed(flagship_profiles):
    _raw_profile(flagship_profiles, "worker-prov", "model:\n  provider: fleet\n")
    out = kc.run_slash("create 'GATE-PROBE' --assignee worker-prov")
    assert "cannot resolve worker-prov model default" in out, out
    assert _card_count() == 0
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="legacy", assignee=None)
        conn.execute("UPDATE tasks SET assignee = 'worker-prov' WHERE id = ?", (task_id,))
        conn.commit()
        assert task_id in kbd.dispatch_once(conn, dry_run=True).flagship_refused


# ---- Prism r7 bf4b5b6f26f8: only an OPERATOR-authored override authorizes ----
# A worker can post any comment body under its own name (kanban_comment); the
# reassign / pin-clear gates must not read that as an operator's authorization.

def _plain_card() -> str:
    with kb.connect() as conn:
        return kb.create_task(conn, title="plain", assignee="daedalus-opus")


@pytest.mark.parametrize("author,run_id", [
    ("daedalus-opus", 7),          # what the kanban_comment tool records for a worker
    ("daedalus-opus", None),       # a worker CLI comment outside its run context
    ("unverified:default", None),  # an operator name the caller could not prove
    ("apollo", 7),                 # an operator label written from inside a worker run
])
def test_worker_authored_override_comment_does_not_authorize_reassign(
    flagship_profiles, author, run_id,
):
    task_id = _plain_card()
    with kb.connect() as conn:
        kb.add_comment(conn, task_id, author, "flagship override: self approved",
                       run_id=run_id)
        assert not kb._card_flagship_authorized(conn, task_id)
        with pytest.raises(kb.FlagshipAssigneeRefused, match="assignee profile default"):
            kb.reassign_task(conn, task_id, "daedalus-fable")
        assert kb.get_task(conn, task_id).assignee == "daedalus-opus"
    out = kc.run_slash(f"reassign {task_id} daedalus-fable")
    assert "assignee profile default" in out, out


@pytest.mark.parametrize("author", ["apollo", "default", "human:ace", "user", "operator"])
def test_operator_authored_override_comment_authorizes_reassign(flagship_profiles, author):
    task_id = _plain_card()
    with kb.connect() as conn:
        kb.add_comment(conn, task_id, author, "flagship override: approved")
        assert kb._card_flagship_authorized(conn, task_id)
    out = kc.run_slash(f"reassign {task_id} daedalus-fable")
    assert "daedalus-fable" in out and "assignee profile default" not in out, out


def test_own_create_with_reason_writes_an_authorizing_comment(
    flagship_profiles, proven_operator,
):
    task_id = _created_id(kc.run_slash(
        "create 'GATE' --assignee daedalus-fable --allow-flagship 'infra'"))
    with kb.connect() as conn:
        db_id = kb.create_task(conn, title="db", assignee="daedalus-fable",
                               flagship_override_reason="infra")
        assert kb._card_flagship_authorized(conn, task_id)
        assert kb._card_flagship_authorized(conn, db_id)


def test_unproven_operator_create_override_does_not_preauthorize_reassign(flagship_profiles):
    """An unproven CLI caller's override is ``unverified:default``: dispatch
    still reads the card's comment, but a later reassign/clear needs its own
    --allow-flagship."""
    task_id = _created_id(kc.run_slash(
        "create 'GATE' --assignee daedalus-fable --allow-flagship 'infra'"))
    with kb.connect() as conn:
        authors = [c.author for c in kb.list_comments(conn, task_id)]
        assert len(authors) == 1 and authors[0].startswith("unverified:"), authors
        assert not kb._card_flagship_authorized(conn, task_id)
