"""Policy gates for flagship routes: an explicit ``--model`` pin, or an
assignee whose profile default is a flagship (t_9e7ba884).

ONE authority (``model_policy.flagship_authority``: an operator label outside
a dispatched worker), ONE writer (``kanban_db.authorize_flagship``), ONE
reader (``kanban_db._card_flagship_authorized``). The tests are the matrix
{actor} x {verb}: every write path refuses the same actors the read side
distrusts, so an accepted override is always one dispatch honours.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from hermes_cli import kanban as kc
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli.model_policy import (
    FLAGSHIP_AUTHORITY_ERROR,
    assignee_default_model,
    firepower_guard_error,
    route_kind,
)

FABLE = "claude-fable-5-1"
REASON = "delicate infra: dispatcher lock rewrite"
OVERRIDE = f"flagship override: {REASON}"


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    for var in ("HERMES_PROFILE_NAME", "HERMES_PROFILE", "HERMES_KANBAN_TASK"):
        monkeypatch.delenv(var, raising=False)
    kb.init_db()
    return home


def _created_id(output: str) -> str:
    match = re.search(r"(t_[a-f0-9]+)", output)
    assert match, output
    return match.group(1)


def _profile(kanban_home, name: str, body: str) -> None:
    profile = kanban_home / "profiles" / name
    profile.mkdir(parents=True, exist_ok=True)
    (profile / "config.yaml").write_text(body, encoding="utf-8")


def _default(model: str) -> str:
    return f"model:\n  default: {model}\n  provider: claude-apr\n"


@pytest.fixture
def flagship_profiles(kanban_home):
    _profile(kanban_home, "daedalus-fable", _default(FABLE))
    _profile(kanban_home, "daedalus-astra", _default("gpt-6-astra-900k"))
    _profile(kanban_home, "daedalus-opus", _default("claude-opus-5-5"))
    _profile(kanban_home, "daedalus-sonnet", _default("claude-sonnet-5"))
    return kanban_home


def _comments(task_id: str) -> list[tuple[str, str]]:
    with kb.connect() as conn:
        return [(c.author, c.body) for c in kb.list_comments(conn, task_id)]


def _refused_at_dispatch(task_id: str) -> bool:
    with kb.connect() as conn:
        return task_id in kbd.dispatch_once(conn, dry_run=True).flagship_refused


def _card_count() -> int:
    with kb.connect() as conn:
        return conn.execute("SELECT count(*) FROM tasks").fetchone()[0]


def _card(assignee: str, model: str | None = None, **kw) -> str:
    with kb.connect() as conn:
        return kb.create_task(conn, title=assignee, assignee=assignee, model_override=model, **kw)


def _legacy_flagship_card() -> str:
    """A card that reached its flagship profile by a path the gate never saw."""
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="legacy", assignee=None)
        conn.execute("UPDATE tasks SET assignee = 'daedalus-fable' WHERE id = ?", (task_id,))
        conn.commit()
    return task_id


# --- main's predicate -----------------------------------------------------------


@pytest.mark.parametrize("model", ["gpt-6-astra-900k", "openai-codex/gpt-6-astra-900k", FABLE])
def test_guard_requires_nonempty_reason_for_flagship(model):
    assert "--firepower" in firepower_guard_error(model, None)
    assert "--firepower" in firepower_guard_error(model, "  ")
    assert firepower_guard_error(model, "hard architecture adjudication") is None


def test_guard_does_not_restrict_sub_flagship():
    assert firepower_guard_error("gpt-5.6-sol-900k", None) is None
    assert firepower_guard_error("claude-opus-5", None) is None
    assert route_kind("openai-codex/gpt-5.6-sol-900k") == "standard"
    assert route_kind("openai-codex/gpt-6-astra-900k") == "firepower"


def test_effective_worker_route_reads_profile_default_and_prefers_pin(kanban_home):
    _profile(kanban_home, "worker",
             "model:\n  default: openai-codex/gpt-5.6-sol-900k\n  provider: openai-codex\n")
    with kb.connect() as conn:
        plain = kb.get_task(conn, kb.create_task(conn, title="plain", assignee="worker"))
        pinned = kb.get_task(conn, kb.create_task(
            conn, title="pinned", assignee="worker",
            model_override="gpt-5.6-sol-900k", provider_override="openai-codex"))
    assert kb.effective_worker_route(plain) == "openai-codex/gpt-5.6-sol-900k"
    assert kb.effective_worker_route(pinned) == "openai-codex/gpt-5.6-sol-900k"


def test_dispatch_tick_records_route_for_each_spawn(kanban_home, monkeypatch):
    from hermes_cli import profiles

    _profile(kanban_home, "worker",
             "model:\n  default: openai-codex/gpt-5.6-sol-900k\n  provider: openai-codex\n")
    monkeypatch.setattr(profiles, "profile_exists", lambda _name: True)
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="route announce", assignee="worker")
        result = kbd.dispatch_once(conn, spawn_fn=lambda _t, _w: 12345, reconcile_orphans=False)
    assert result.spawn_routes == {task_id: "openai-codex/gpt-5.6-sol-900k"}


# --- no reason: every flagship route is refused, every other route is not ------


@pytest.mark.parametrize("assignee", ["daedalus-fable", "daedalus-astra"])
def test_create_refuses_flagship_profile_assignee_without_reason(flagship_profiles, assignee):
    out = kc.run_slash(f"create 'GATE-PROBE' --assignee {assignee}")
    assert "--allow-flagship" in out and "assignee profile default" in out, out
    assert _card_count() == 0


@pytest.mark.parametrize("verb", ["reassign", "assign"])
def test_reassign_to_flagship_profile_refuses_without_reason(flagship_profiles, verb):
    task_id = _card("daedalus-opus")
    out = kc.run_slash(f"{verb} {task_id} daedalus-fable")
    assert "assignee profile default" in out and "--allow-flagship" in out, out
    with kb.connect() as conn:
        assert kb.get_task(conn, task_id).assignee == "daedalus-opus"
        with pytest.raises(kb.FlagshipAssigneeRefused, match="assignee profile default"):
            kb.reassign_task(conn, task_id, "daedalus-fable")
    assert _comments(task_id) == []


def test_set_model_clear_onto_flagship_default_refuses_before_reclaim(flagship_profiles, monkeypatch):
    reclaimed = []
    monkeypatch.setattr(kb, "reclaim_task", lambda *a, **k: reclaimed.append(a) or True)
    task_id = _card("daedalus-fable", "claude-opus-5-5")
    out = kc.run_slash(f"set-model {task_id} --reclaim")
    assert "assignee profile default of 'daedalus-fable'" in out, out
    assert reclaimed == []
    with kb.connect() as conn:
        with pytest.raises(ValueError, match="cannot clear the model pin"):
            kb.set_model_override(conn, task_id, None)
        # A non-flagship audit comment does not stand in for the check (aed001cfb920).
        with pytest.raises(ValueError, match="cannot clear the model pin"):
            kb.set_model_override(conn, task_id, None,
                                  audit_comment_author="apollo", audit_comment_body="rate limit")
        assert kb.get_task(conn, task_id).model_override == "claude-opus-5-5"
    assert _comments(task_id) == []


def test_set_model_flagship_pin_refuses_without_reason(flagship_profiles):
    task_id = _card("daedalus-opus", "claude-opus-5-5")
    assert "--firepower" in kc.run_slash(f"set-model {task_id} {FABLE}")
    with kb.connect() as conn:
        with pytest.raises(ValueError, match="orchestrator-only"):
            kb.set_model_override(conn, task_id, FABLE)
        with pytest.raises(ValueError, match="orchestrator-only"):
            kb.apply_batch_route_writes(conn, [kb.BatchRouteWrite(
                task_id=task_id, touch_model=True, model=FABLE,
                audit_comment_author="apollo", audit_comment_body="some other note")])
        assert kb.get_task(conn, task_id).model_override == "claude-opus-5-5"
    assert _comments(task_id) == []


def test_swarm_flagship_child_without_reason_is_refused(flagship_profiles):
    out = kc.run_slash("swarm 'probe' --worker 'daedalus-fable:w1' "
                       "--verifier daedalus-opus --synthesizer daedalus-opus")
    assert "assignee profile default of 'daedalus-fable'" in out, out
    assert _card_count() == 0


def test_dispatch_refuses_a_legacy_flagship_profile_card(flagship_profiles):
    task_id = _legacy_flagship_card()
    with kb.connect() as conn:
        res = kbd.dispatch_once(conn, spawn_fn=lambda _t, _w: 12345, reconcile_orphans=False)
        assert kb.get_task(conn, task_id).status == "ready"
    assert task_id in res.flagship_refused
    assert any(body.startswith("flagship dispatch refused:")
               and "assignee profile default" in body and FABLE in body
               for _a, body in _comments(task_id)), _comments(task_id)


@pytest.mark.parametrize("assignee", ["daedalus-opus", "daedalus-sonnet", "worker"])
def test_non_flagship_routes_need_no_reason(flagship_profiles, assignee):
    task_id = _created_id(kc.run_slash(f"create 'plain' --assignee {assignee}"))
    pinned = _created_id(kc.run_slash(f"create 'pinned' --assignee daedalus-fable --model claude-opus-5-5"))
    with kb.connect() as conn:
        assert kb.get_task(conn, pinned).model_override == "claude-opus-5-5"
        assert kb.set_model_override(conn, pinned, "claude-sonnet-5")
        other = kb.create_task(conn, title="o", assignee="daedalus-fable", model_override="claude-opus-5-5")
        assert kb.set_model_override(conn, _card("daedalus-opus", "claude-sonnet-5"), None)
    assert _comments(task_id) == [] and _comments(pinned) == []
    assert not _refused_at_dispatch(task_id) and not _refused_at_dispatch(pinned)
    assert kc.run_slash(f"reassign {other} {assignee}").startswith("Reassigned")


# --- the authority matrix: {actor} x {verb}, each with a reason ------------------
# flagship_authority(actor) = is_operator_label(actor) and not is_dispatched_worker().
# Only "operator" holds it; every other actor is refused by every write path with
# FLAGSHIP_AUTHORITY_ERROR, before any mutation. What the operator writes is
# exactly what the reader admits, so the card dispatches.

ACTORS = ["operator", "orchestrator", "worker", "worker_as_operator", "unverified"]


@pytest.fixture
def actor(request, monkeypatch, flagship_profiles):
    """One cell of the actor axis; returns ``(name, parent_card)``."""
    from hermes_cli import kanban_identity

    name = request.param
    parent = _card("daedalus-opus")
    proven = name in ("operator", "worker_as_operator")
    monkeypatch.setattr(kanban_identity, "operator_label_proven", lambda _n: proven)
    monkeypatch.setenv("HERMES_PROFILE",
                       "apollo" if name in ("operator", "unverified", "worker_as_operator")
                       else "daedalus-opus")
    if name.startswith("worker"):
        monkeypatch.setenv("HERMES_KANBAN_TASK", parent)  # a dispatched worker
    return name, parent


def _assert_untouched(task_id: str, assignee: str, model: str | None, count: int) -> None:
    with kb.connect() as conn:
        task = kb.get_task(conn, task_id)
    assert (task.assignee, task.model_override) == (assignee, model)
    assert _comments(task_id) == []
    assert _card_count() == count


def _assert_authorized(task_id: str) -> None:
    (author, body), = _comments(task_id)
    assert body == OVERRIDE
    from hermes_cli.kanban_identity import is_operator_label

    assert is_operator_label(author), author
    with kb.connect() as conn:
        assert kb._card_flagship_authorized(conn, task_id)
    assert not _refused_at_dispatch(task_id)


@pytest.mark.parametrize("actor", ACTORS, indirect=True)
@pytest.mark.parametrize("route", ["--assignee daedalus-fable", f"--assignee daedalus-opus --model {FABLE}"])
def test_create_with_reason(actor, route):
    name, parent = actor
    before = _card_count()
    out = kc.run_slash(f"create 'GATE' {route} --parent {parent} --allow-flagship '{REASON}'")
    if name != "operator":
        assert FLAGSHIP_AUTHORITY_ERROR in out, out
        assert _card_count() == before
        with kb.connect() as conn, pytest.raises(ValueError, match="operator/orchestrator-only"):
            kb.create_task(conn, title="db", assignee="daedalus-fable", parents=[parent],
                           flagship_override_reason=REASON)
        assert _card_count() == before
        return
    _assert_authorized(_created_id(out))
    with kb.connect() as conn:
        db_id = kb.create_task(conn, title="db", assignee="daedalus-fable", flagship_override_reason=REASON)
    _assert_authorized(db_id)


@pytest.mark.parametrize("actor", ACTORS, indirect=True)
@pytest.mark.parametrize("verb", ["reassign", "assign"])
def test_reassign_with_reason(actor, verb):
    name, _parent = actor
    task_id = _card("daedalus-opus")
    count = _card_count()
    out = kc.run_slash(f"{verb} {task_id} daedalus-fable --allow-flagship '{REASON}'")
    if name != "operator":
        assert FLAGSHIP_AUTHORITY_ERROR in out, out
        with kb.connect() as conn, pytest.raises(kb.FlagshipAssigneeRefused, match="operator/orchestrator-only"):
            kb.reassign_task(conn, task_id, "daedalus-fable", flagship_override_reason=REASON)
        _assert_untouched(task_id, "daedalus-opus", None, count)
        return
    assert "daedalus-fable" in out, out
    with kb.connect() as conn:
        assert kb.get_task(conn, task_id).assignee == "daedalus-fable"
    _assert_authorized(task_id)
    # The same call again records nothing new (Prism 70a0613a6b24).
    kc.run_slash(f"{verb} {task_id} daedalus-fable --allow-flagship '{REASON}'")
    assert len(_comments(task_id)) == 1


@pytest.mark.parametrize("actor", ACTORS, indirect=True)
def test_pin_clear_with_reason(actor):
    name, _parent = actor
    task_id = _card("daedalus-fable", "claude-opus-5-5")
    count = _card_count()
    out = kc.run_slash(f"set-model {task_id} --allow-flagship '{REASON}'")
    if name != "operator":
        assert FLAGSHIP_AUTHORITY_ERROR in out, out
        with kb.connect() as conn, pytest.raises(ValueError, match="operator/orchestrator-only"):
            kb.set_model_override(conn, task_id, None, flagship_override_reason=REASON)
        _assert_untouched(task_id, "daedalus-fable", "claude-opus-5-5", count)
        return
    with kb.connect() as conn:
        assert kb.get_task(conn, task_id).model_override is None, out
    _assert_authorized(task_id)
    db_id = _card("daedalus-fable", "claude-opus-5-5")
    with kb.connect() as conn:
        assert kb.set_model_override(conn, db_id, None, flagship_override_reason=REASON)
    _assert_authorized(db_id)


@pytest.mark.parametrize("actor", ACTORS, indirect=True)
def test_pin_set_with_reason(actor):
    name, _parent = actor
    task_id = _card("daedalus-opus", "claude-opus-5-5")
    count = _card_count()
    out = kc.run_slash(f"set-model {task_id} {FABLE} --allow-flagship '{REASON}'")
    if name != "operator":
        assert FLAGSHIP_AUTHORITY_ERROR in out, out
        with kb.connect() as conn, pytest.raises(ValueError, match="operator/orchestrator-only"):
            kb.set_model_override(conn, task_id, FABLE, flagship_override_reason=REASON)
        _assert_untouched(task_id, "daedalus-opus", "claude-opus-5-5", count)
        return
    with kb.connect() as conn:
        assert kb.get_task(conn, task_id).model_override == FABLE, out
    _assert_authorized(task_id)
    db_id = _card("daedalus-opus", "claude-opus-5-5")
    with kb.connect() as conn:
        assert kb.set_model_override(conn, db_id, FABLE, flagship_override_reason=REASON)
    _assert_authorized(db_id)


@pytest.mark.parametrize("actor", ACTORS, indirect=True)
def test_lane_override_with_firepower(actor):
    name, _parent = actor
    out = kc.run_slash("lane-model set openai-codex/gpt-6-astra-900k --assignee daedalus-opus "
                       f"--ttl 2h --reason capacity --firepower '{REASON}'")
    with kb.connect() as conn:
        lane = kb.get_lane_model_override(conn, assignee="daedalus-opus")
    if name != "operator":
        assert FLAGSHIP_AUTHORITY_ERROR in out, out
        assert lane is None
        with kb.connect() as conn, pytest.raises(ValueError, match="operator/orchestrator-only"):
            kb.set_lane_model_override(conn, provider="openai-codex", model="gpt-6-astra-900k",
                                       expires_at=4_000_000_000, assignee="daedalus-opus",
                                       firepower=REASON)
            assert kb.get_lane_model_override(conn, assignee="daedalus-opus") is None
        return
    assert lane is not None and lane.firepower == REASON, out
    from hermes_cli.kanban_identity import is_operator_label

    assert is_operator_label(lane.created_by), lane.created_by  # the resolved actor, persisted
    assert not _refused_at_dispatch(_card("daedalus-opus"))


@pytest.mark.parametrize("created_by", ["daedalus-opus", "unverified:apollo", "operator", "", None])
def test_lane_row_with_a_non_operator_actor_authorizes_nothing(flagship_profiles, proven_operator, created_by):
    """The lane reader (Prism 89f43d1114a4): a ``lane_model_overrides`` row
    whose firepower was recorded before the write gate, or planted by hand,
    under a worker / unverified / empty actor admits no card. Fail closed, no
    migration; the card's own operator override still does."""
    with kb.connect() as conn:
        conn.execute(
            "INSERT INTO lane_model_overrides (assignee, provider, model, reason, firepower, "
            "created_by, created_at, expires_at) VALUES ('daedalus-opus', 'openai-codex', "
            "'gpt-6-astra-900k', 'capacity', 'self approved', ?, 1, 4000000000)",
            (created_by,),
        )
        conn.commit()
        lane = kb.get_lane_model_override(conn, assignee="daedalus-opus")
    assert lane is not None and not lane.flagship_authorized()
    card = _card("daedalus-opus")
    assert _refused_at_dispatch(card)
    with kb.connect() as conn:
        kb.add_comment(conn, card, "apollo", OVERRIDE)
    assert not _refused_at_dispatch(card)


@pytest.mark.parametrize("actor", ACTORS, indirect=True)
def test_swarm_with_reason(actor):
    name, _parent = actor
    before = _card_count()
    # --created-by names the bookkeeping root's assignee; the actor the gate
    # judges is still the caller's own identity.
    out = kc.run_slash("swarm 'probe' --worker 'daedalus-fable:w1' --verifier daedalus-opus "
                       f"--synthesizer daedalus-opus --created-by daedalus-opus "
                       f"--allow-flagship '{REASON}' --json")
    if name != "operator":
        assert FLAGSHIP_AUTHORITY_ERROR in out, out
        assert _card_count() == before
        return
    created = json.loads(out)
    _assert_authorized(created["worker_ids"][0])
    # A non-flagship child gets no override from the shared reason; the root is
    # bookkeeping (no_worker), never a worker route.
    assert _comments(created["verifier_id"]) == []
    with kb.connect() as conn:
        root = kb.get_task(conn, created["root_id"])
        assert root.status == "done" and not _refused_at_dispatch(root.id)


# --- the reader: which recorded comments authorize ---------------------------------


@pytest.mark.parametrize("author", ["apollo", "default", "human:ace", "user"])
def test_operator_authored_override_admits_everywhere(flagship_profiles, proven_operator, author):
    profile_card, pinned = _legacy_flagship_card(), _card("daedalus-opus")
    plain = _card("daedalus-opus")
    with kb.connect() as conn:
        conn.execute("UPDATE tasks SET model_override = ? WHERE id = ?", (FABLE, pinned))
        conn.commit()
        for task_id in (profile_card, pinned, plain):
            kb.add_comment(conn, task_id, author, "flagship override: approved")
            assert kb._card_flagship_authorized(conn, task_id)
    assert not _refused_at_dispatch(profile_card) and not _refused_at_dispatch(pinned)
    out = kc.run_slash(f"reassign {plain} daedalus-fable")
    assert "daedalus-fable" in out and "assignee profile default" not in out, out


@pytest.mark.parametrize("author,run_id", [
    ("daedalus-opus", 7),          # what the kanban_comment tool records for a worker
    ("daedalus-opus", None),       # a worker CLI comment outside its run context
    ("unverified:default", None),  # an operator name the caller could not prove
    ("apollo", 7),                 # an operator label written from inside a worker run
    ("operator", None),            # the literal; not an operator label (9893c2209071)
])
def test_untrusted_override_comment_admits_nowhere(flagship_profiles, author, run_id):
    """The reader, on its own: a row the write gate would never have produced
    (planted by raw SQL, as a legacy board or a hand edit could) still
    authorizes nothing."""
    profile_card, pinned = _legacy_flagship_card(), _card("daedalus-opus")
    plain = _card("daedalus-opus")
    with kb.connect() as conn:
        conn.execute("UPDATE tasks SET model_override = ? WHERE id = ?", (FABLE, pinned))
        for task_id in (profile_card, pinned, plain):
            conn.execute(
                "INSERT INTO task_comments (task_id, author, body, run_id, created_at) "
                "VALUES (?, ?, 'flagship override: self approved', ?, 1)",
                (task_id, author, run_id),
            )
        conn.commit()
        for task_id in (profile_card, pinned, plain):
            assert not kb._card_flagship_authorized(conn, task_id)
        with pytest.raises(kb.FlagshipAssigneeRefused, match="assignee profile default"):
            kb.reassign_task(conn, plain, "daedalus-fable")
    assert _refused_at_dispatch(profile_card) and _refused_at_dispatch(pinned)
    assert "assignee profile default" in kc.run_slash(f"reassign {plain} daedalus-fable")


# --- the route the gate judges: what the worker will RUN ---------------------------


def _assert_gated(assignee: str, message: str = "assignee profile default") -> None:
    out = kc.run_slash(f"create 'GATE-PROBE' --assignee {assignee}")
    assert message in out and "--allow-flagship" in out, out
    assert _card_count() == 0
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="legacy", assignee=None)
        conn.execute("UPDATE tasks SET assignee = ? WHERE id = ?", (assignee, task_id))
        conn.commit()
    assert _refused_at_dispatch(task_id)


def _assert_not_gated(assignee: str) -> None:
    task_id = _created_id(kc.run_slash(f"create 'plain' --assignee {assignee}"))
    assert _comments(task_id) == [] and not _refused_at_dispatch(task_id)


@pytest.mark.parametrize("aliases", [
    "  aliases:\n    fast: claude-apr/claude-fable-5-1\n",
    "  aliases:\n    fast: {model: claude-fable-5-1, provider: claude-apr}\n",
    "model_aliases:\n  fast: {model: claude-fable-5-1, provider: claude-apr}\n",
])
def test_profile_alias_resolving_to_flagship_is_gated(flagship_profiles, aliases):
    _profile(flagship_profiles, "worker-fast", "model:\n  default: fast\n  provider: claude-apr\n" + aliases)
    _assert_gated("worker-fast")


def test_profile_alias_named_like_flagship_but_resolving_to_opus_is_not_gated(flagship_profiles):
    _profile(flagship_profiles, "worker-lite", "model:\n  default: fable-lite\n  provider: claude-apr\n"
             "  aliases:\n    fable-lite: claude-apr/claude-opus-5-5\n")
    _assert_not_gated("worker-lite")


_REMAP = ("model:\n  default: claude-sonnet-5\n  provider: claude-apr\n"
          f"  aliases:\n    claude-opus-5-5: claude-apr/{FABLE}\n")


def test_pin_remapped_to_flagship_by_the_assignee_aliases_is_gated(flagship_profiles, proven_operator):
    """Prism a1f3096286e2: the spawned worker re-resolves ``-m <pin>`` through
    ITS OWN profile aliases, so an Opus pin on a profile aliasing Opus to Fable
    runs Fable. The pin is judged as the assignee RUNS it: create, set-model
    (single + batch), reassign and the dispatcher's pin branch all refuse it
    without ``--allow-flagship``; with one, the override is recorded."""
    _profile(flagship_profiles, "worker-remap", _REMAP)
    out = kc.run_slash("create 'x' --assignee worker-remap --model claude-opus-5-5")
    assert "--allow-flagship" in out and "worker-remap" in out and FABLE in out, out
    assert _card_count() == 0

    task_id = _card("worker-remap")  # the profile default (sonnet) is not gated
    assert FABLE in kc.run_slash(f"set-model {task_id} claude-opus-5-5")
    with kb.connect() as conn:
        with pytest.raises(ValueError, match=FABLE):
            kb.set_model_override(conn, task_id, "claude-opus-5-5")
        with pytest.raises(ValueError, match=FABLE):
            kb.apply_batch_route_writes(conn, [kb.BatchRouteWrite(
                task_id=task_id, touch_model=True, model="claude-opus-5-5")])
        assert kb.get_task(conn, task_id).model_override is None
        assert kb.set_model_override(conn, task_id, "claude-sonnet-5")  # not aliased: fine
    assert _comments(task_id) == []

    pinned = _card("daedalus-opus", "claude-opus-5-5")
    assert FABLE in kc.run_slash(f"reassign {pinned} worker-remap")
    with kb.connect() as conn:
        assert kb.get_task(conn, pinned).assignee == "daedalus-opus"
        legacy = kb.create_task(conn, title="legacy", assignee="worker-remap")
        conn.execute("UPDATE tasks SET model_override = 'claude-opus-5-5' WHERE id = ?", (legacy,))
        conn.commit()
    assert _refused_at_dispatch(legacy)

    ok = _created_id(kc.run_slash(
        "create 'x' --assignee worker-remap --model claude-opus-5-5 --allow-flagship 'known lane'"))
    assert [b for _a, b in _comments(ok)] == ["flagship override: known lane"]
    assert not _refused_at_dispatch(ok)
    with kb.connect() as conn:
        assert kb.set_model_override(conn, task_id, "claude-opus-5-5", flagship_override_reason="known lane")
    assert [b for _a, b in _comments(task_id)] == ["flagship override: known lane"]
    assert not _refused_at_dispatch(task_id)


@pytest.mark.parametrize("caller_env,dotenv,gated", [
    (None, FABLE, True),               # the var lives in the assignee's own .env
    ("claude-opus-5-5", FABLE, True),  # the assignee's scope beats the caller's env
    (FABLE, "claude-opus-5-5", False),
])
def test_env_var_default_resolves_in_the_assignee_scope(flagship_profiles, monkeypatch, caller_env, dotenv, gated):
    if caller_env is None:
        monkeypatch.delenv("T9E_WORKER_MODEL", raising=False)
    else:
        monkeypatch.setenv("T9E_WORKER_MODEL", caller_env)
    _profile(flagship_profiles, "worker-env", "model:\n  default: ${T9E_WORKER_MODEL}\n  provider: claude-apr\n")
    (flagship_profiles / "profiles" / "worker-env" / ".env").write_text(
        f"T9E_WORKER_MODEL={dotenv}\n", encoding="utf-8")
    _assert_gated("worker-env") if gated else _assert_not_gated("worker-env")


def test_managed_overlay_default_is_the_gated_route(flagship_profiles, monkeypatch, tmp_path):
    from hermes_cli import managed_scope

    managed = tmp_path / "managed"
    managed.mkdir()
    (managed / "config.yaml").write_text(f"model:\n  default: {FABLE}\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_MANAGED_DIR", str(managed))
    managed_scope.invalidate_managed_cache()
    try:
        _assert_gated("daedalus-opus")  # its own file says opus; the managed pin wins
    finally:
        monkeypatch.delenv("HERMES_MANAGED_DIR")
        managed_scope.invalidate_managed_cache()


_FLEET_PROVIDER = ("model:\n  provider: fleet\nproviders:\n  fleet:\n"
                   "    base_url: http://127.0.0.1:9/v1\n    default_model: {model}\n")


@pytest.mark.parametrize("body", [
    _FLEET_PROVIDER.format(model=FABLE),
    "model:\n  provider: fleet\ncustom_providers:\n"
    f"  - name: fleet\n    base_url: http://127.0.0.1:9/v1\n    model: {FABLE}\n",
])
def test_provider_default_model_is_the_gated_route(flagship_profiles, body):
    _profile(flagship_profiles, "worker-prov", body)
    _assert_gated("worker-prov")


def test_provider_default_resolves_through_the_startup_lookup(flagship_profiles):
    """Prism 8aa2ca1bca8a: a ``providers:`` entry with no endpoint is skipped
    by worker startup (``_match_new_style_provider``), which then runs the
    legacy ``custom_providers`` entry of the same name. The gate must read the
    route the worker RUNS, through that same lookup: Fable here, not the
    endpoint-less entry's Opus."""
    _profile(flagship_profiles, "worker-prov",
             "model:\n  provider: fleet\nproviders:\n  fleet:\n    default_model: claude-opus-5-5\n"
             f"custom_providers:\n  - name: fleet\n    base_url: http://127.0.0.1:9/v1\n    model: {FABLE}\n")
    assert assignee_default_model("worker-prov") == FABLE
    _assert_gated("worker-prov")


def test_provider_default_model_is_literal_and_opus_is_not_gated(flagship_profiles):
    _profile(flagship_profiles, "worker-prov", _FLEET_PROVIDER.format(model="fast")
             + f"model_aliases:\n  fast:\n    model: {FABLE}\n    provider: claude-apr\n")
    assert assignee_default_model("worker-prov") == "fast"  # 00f1687144ac: never alias-resolved
    _profile(flagship_profiles, "worker-prov", _FLEET_PROVIDER.format(model="claude-opus-5-5"))
    _assert_not_gated("worker-prov")


@pytest.mark.parametrize("name,body", [
    ("worker-unres", "model:\n  default: ${T9E_UNSET_MODEL}\n  provider: claude-apr\n"),
    ("worker-prov", "model:\n  provider: fleet\n"),
    ("worker-badyaml", "model: [unclosed\n  default: x\n"),
])
def test_unresolved_or_unreadable_default_fails_closed(flagship_profiles, monkeypatch, proven_operator, name, body):
    monkeypatch.delenv("T9E_UNSET_MODEL", raising=False)
    _profile(flagship_profiles, name, body)
    _assert_gated(name, f"cannot resolve {name} model default")
    ok = _created_id(kc.run_slash(f"create 'x' --assignee {name} --allow-flagship 'known lane'"))
    assert [b for _a, b in _comments(ok)] == ["flagship override: known lane"]


def test_profile_config_read_oserror_fails_closed(flagship_profiles, monkeypatch):
    from hermes_cli import config as vcfg

    _profile(flagship_profiles, "worker-eio", _default("claude-opus-5-5"))
    real = vcfg.read_user_config_raw

    def flaky(path=None):
        if path is not None and "worker-eio" in str(path):
            raise OSError(5, "Input/output error")
        return real(path)

    monkeypatch.setattr(vcfg, "read_user_config_raw", flaky)
    _assert_gated("worker-eio", "cannot resolve worker-eio model default")


def test_missing_profile_and_human_stay_ungated(flagship_profiles):
    assert assignee_default_model("no-such-profile") is None
    assert assignee_default_model("human") is None
    assert assignee_default_model("human:ace") is None


def test_create_judges_the_final_assignee_after_placeholder_remap(flagship_profiles, monkeypatch):
    """A dispatched worker minting a child: placeholder assignees remap to
    ``kanban.default_assignee`` and THAT route is gated (311c94f92990)."""
    from hermes_cli import kanban_worker_policy as kwp

    parent = _card("daedalus-opus")
    monkeypatch.setenv("HERMES_KANBAN_TASK", parent)
    monkeypatch.setattr(kwp, "_load_config", lambda: {"kanban": {"default_assignee": "daedalus-fable"}})
    before = _card_count()
    with kb.connect() as conn:
        with pytest.raises(ValueError, match="assignee profile default of 'daedalus-fable'"):
            kb.create_task(conn, title="child", assignee="human", parents=[parent])
    assert _card_count() == before
    _profile(flagship_profiles, "apollo", _default(FABLE))
    monkeypatch.setattr(kwp, "_load_config", lambda: {"kanban": {"default_assignee": "daedalus-opus"}})
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="child", assignee="apollo", parents=[parent])
        assert kb.get_task(conn, task_id).assignee == "daedalus-opus"
    assert _comments(task_id) == []


def test_lane_model_remapped_to_flagship_by_the_assignee_aliases_is_refused(flagship_profiles, proven_operator):
    """Prism 0ed436221a5c: the lane row is handed to the worker as ``-m
    <lane.model>`` and re-resolved through the ASSIGNEE's aliases, exactly
    like a card pin (a1f3096286e2). An Opus lane (no firepower authority
    needed to install it) on a profile aliasing Opus to Fable runs Fable, so
    the dispatcher judges the lane as the assignee RUNS it: refused, with a
    comment naming the remap; the card's own operator override admits it."""
    _profile(flagship_profiles, "worker-remap", _REMAP)
    with kb.connect() as conn:
        kb.set_lane_model_override(conn, provider="claude-apr", model="claude-opus-5-5",
                                   expires_at=4_000_000_000, assignee="worker-remap")
    card = _card("worker-remap")
    assert _refused_at_dispatch(card)
    with kb.connect() as conn:
        kbd.dispatch_once(conn, dry_run=False)
    bodies = [b for _a, b in _comments(card)]
    assert len(bodies) == 1 and "claude-opus-5-5" in bodies[0] and FABLE in bodies[0], bodies
    assert "lane-model override" in bodies[0] and "worker-remap" in bodies[0], bodies
    with kb.connect() as conn:
        kb.add_comment(conn, card, "apollo", OVERRIDE)
    assert not _refused_at_dispatch(card)
    # The same lane on a profile with no such alias runs Opus: not gated.
    assert not _refused_at_dispatch(_card("daedalus-opus"))


@pytest.mark.parametrize("default", ["fleet", "Fleet", "custom:fleet"])
def test_provider_name_as_model_default_is_the_provider_default_model(flagship_profiles, default):
    """Prism 10d1b746c9aa: a ``model.default`` that is the provider's own
    slug / display name is not a model; worker startup
    (``cli_agent_setup_mixin``) substitutes the entry's ``default_model``,
    so that is the route the gate judges: Fable here, not the string
    ``"fleet"``."""
    _profile(flagship_profiles, "worker-prov",
             f"model:\n  default: {default}\n  provider: fleet\nproviders:\n  fleet:\n"
             f"    base_url: http://127.0.0.1:9/v1\n    default_model: {FABLE}\n")
    assert assignee_default_model("worker-prov") == FABLE
    _assert_gated("worker-prov")
    # A provider-name default on an entry with no default_model is an unknown route.
    _profile(flagship_profiles, "worker-prov",
             f"model:\n  default: {default}\n  provider: fleet\nproviders:\n  fleet:\n"
             "    base_url: http://127.0.0.1:9/v1\n")
    assert assignee_default_model("worker-prov").startswith("<unresolved:")
    with pytest.raises(kb.FlagshipAssigneeRefused, match="cannot resolve worker-prov model default"):
        with kb.connect() as conn:
            kb.create_task(conn, title="x", assignee="worker-prov")
    _profile(flagship_profiles, "worker-prov",
             f"model:\n  default: {default}\n  provider: fleet\nproviders:\n  fleet:\n"
             "    base_url: http://127.0.0.1:9/v1\n    default_model: claude-opus-5-5\n")
    assert assignee_default_model("worker-prov") == "claude-opus-5-5"
    _assert_not_gated("worker-prov")


def test_pin_clear_onto_a_lane_override_is_the_dispatchers_call(flagship_profiles):
    """A clear whose resulting route is the lane is judged by the dispatcher
    gate on the lane's own authority, not by the pin-clear gate."""
    task_id = _card("daedalus-fable", "claude-opus-5-5")
    with kb.connect() as conn:
        kb.set_lane_model_override(conn, provider="claude-apr", model="claude-opus-5-5",
                                   expires_at=4_000_000_000, assignee="daedalus-fable")
        assert kb.set_model_override(conn, task_id, None)
    assert _comments(task_id) == [] and not _refused_at_dispatch(task_id)


# --- the two forgeable signals (Prism 5e978e7740dd / 7a9e431141ad) ------------------
# A worker can unset HERMES_KANBAN_TASK, and a workers


# --- the two forgeable signals (Prism 5e978e7740dd / 7a9e431141ad) ------------------
# A worker can unset HERMES_KANBAN_TASK, and a worker's comment on ANOTHER card is
# stored with run_id NULL. Neither may yield an override dispatch honours.


def _operator_worker(monkeypatch) -> None:
    """A dispatcher-spawned worker running AS an operator profile: the board
    knows its pid (worker_ancestor_profile finds the run row); the env
    marker is gone (``env -u HERMES_KANBAN_TASK``), and the operator label
    is otherwise proven."""
    from hermes_cli import kanban_identity

    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.setenv("HERMES_PROFILE", "apollo")
    monkeypatch.setattr(kanban_identity, "operator_label_proven", lambda _n: True)
    monkeypatch.setattr(kanban_identity, "worker_ancestor_profile", lambda: "apollo")


@pytest.mark.parametrize("verb", ["create", "reassign", "pin_set", "pin_clear", "lane"])
def test_worker_ancestry_denies_authority_even_without_the_env_marker(flagship_profiles, monkeypatch, verb):
    parent = _card("daedalus-opus")
    _operator_worker(monkeypatch)
    count = _card_count()
    if verb == "create":
        out = kc.run_slash(f"create 'GATE' --assignee daedalus-fable --parent {parent} --allow-flagship '{REASON}'")
        assert _card_count() == count
    elif verb == "reassign":
        task_id = _card("daedalus-opus")
        out = kc.run_slash(f"reassign {task_id} daedalus-fable --allow-flagship '{REASON}'")
        _assert_untouched(task_id, "daedalus-opus", None, count + 1)
    elif verb == "pin_set":
        task_id = _card("daedalus-opus", "claude-opus-5-5")
        out = kc.run_slash(f"set-model {task_id} {FABLE} --allow-flagship '{REASON}'")
        _assert_untouched(task_id, "daedalus-opus", "claude-opus-5-5", count + 1)
    elif verb == "pin_clear":
        task_id = _card("daedalus-fable", "claude-opus-5-5")
        out = kc.run_slash(f"set-model {task_id} --allow-flagship '{REASON}'")
        _assert_untouched(task_id, "daedalus-fable", "claude-opus-5-5", count + 1)
    else:
        out = kc.run_slash("lane-model set openai-codex/gpt-6-astra-900k --assignee daedalus-opus "
                           f"--ttl 2h --reason capacity --firepower '{REASON}'")
        with kb.connect() as conn:
            assert kb.get_lane_model_override(conn, assignee="daedalus-opus") is None
    assert FLAGSHIP_AUTHORITY_ERROR in out, out


def test_operator_outside_any_worker_keeps_authority(flagship_profiles, monkeypatch):
    """The positive control for the ancestry walk: no worker above the operator."""
    from hermes_cli import kanban_identity

    monkeypatch.setenv("HERMES_PROFILE", "apollo")
    monkeypatch.setattr(kanban_identity, "operator_label_proven", lambda _n: True)
    monkeypatch.setattr(kanban_identity, "worker_ancestor_profile", lambda: None)
    out = kc.run_slash(f"create 'GATE' --assignee daedalus-fable --allow-flagship '{REASON}'")
    _assert_authorized(_created_id(out))


@pytest.mark.parametrize("author,worker", [
    ("daedalus-opus", True),       # a worker, its own label, on another card
    ("apollo", True),              # a worker whose profile is an operator label
    ("unverified:apollo", False),  # an operator name the caller could not prove
    ("operator", False),           # the literal
])
def test_override_body_is_refused_at_the_write_for_every_untrusted_author(flagship_profiles, monkeypatch, author, worker):
    """add_comment / _insert_comment refuse a ``flagship override:`` body from
    anyone but an operator outside a worker, so a cross-task comment (run_id
    NULL by design) can never plant the row the reader trusts."""
    card_a, card_b = _card("daedalus-opus"), _legacy_flagship_card()
    if worker:
        monkeypatch.setenv("HERMES_KANBAN_TASK", card_a)
    with kb.connect() as conn:
        with pytest.raises(kb.FlagshipAssigneeRefused, match="operator/orchestrator-only"):
            kb.add_comment(conn, card_b, author, "flagship override: self approved")
        with pytest.raises(kb.FlagshipAssigneeRefused, match="operator/orchestrator-only"):
            kb._insert_comment(conn, card_b, author, "  Flagship Override: self approved", 1)
        assert not kb._card_flagship_authorized(conn, card_b)
    assert _comments(card_b) == []
    assert _refused_at_dispatch(card_b)
    # Any other body from the same author still lands: comments are the handoff channel.
    with kb.connect() as conn:
        kb.add_comment(conn, card_b, author, "handoff: see card A")
    assert len(_comments(card_b)) == 1


def test_worker_tool_comment_on_another_card_cannot_authorize(flagship_profiles, monkeypatch):
    """The exact repro (7a9e431141ad): a ``default``-profile worker on card A
    posts the override on card B through the kanban_comment tool."""
    from tools import kanban_tools as kt

    card_a, card_b = _card("daedalus-opus"), _legacy_flagship_card()
    with kb.connect() as conn:
        kb.claim_task(conn, card_a)
        run_id = kb._current_run_id(conn, card_a)
    monkeypatch.setenv("HERMES_KANBAN_TASK", card_a)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run_id))
    monkeypatch.setenv("HERMES_PROFILE", "default")
    out = json.loads(kt._handle_comment({"task_id": card_b, "body": "flagship override: self approved"}))
    assert "operator/orchestrator-only" in out.get("error", ""), out
    assert _comments(card_b) == [] and _refused_at_dispatch(card_b)


def test_cli_comment_refuses_an_override_body_from_a_worker(flagship_profiles, monkeypatch):
    """`hermes kanban comment` from a worker shell: the override body is refused
    with a clean rc, any other body lands."""
    card_a, card_b = _card("daedalus-opus"), _legacy_flagship_card()
    monkeypatch.setenv("HERMES_KANBAN_TASK", card_a)
    monkeypatch.setenv("HERMES_PROFILE", "daedalus-opus")
    out = kc.run_slash(f"comment {card_b} 'flagship override: self approved'")
    assert FLAGSHIP_AUTHORITY_ERROR in out, out
    assert _comments(card_b) == [] and _refused_at_dispatch(card_b)
    assert "Comment added" in kc.run_slash(f"comment {card_b} 'handoff: see card A'")


# --- an explicit actor is a claim, not a proof (Prism c5514fd79fb5) ----------------


@pytest.mark.parametrize("explicit", ["apollo", "default", "human:ace"])
def test_explicit_operator_actor_from_an_unproven_process_is_refused(flagship_profiles, monkeypatch, explicit):
    """``authorize_flagship(conn, id, "apollo", reason)`` from a process that
    cannot prove the operator label is coerced to the caller's own verified
    identity (``daedalus-opus`` here) and refused: no row is minted."""
    from hermes_cli import kanban_identity, model_policy

    monkeypatch.setenv("HERMES_PROFILE", "daedalus-opus")
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.setattr(kanban_identity, "operator_label_proven", lambda _n: False)
    monkeypatch.setattr(kanban_identity, "worker_ancestor_profile", lambda: None)
    assert model_policy.flagship_actor(explicit) == "daedalus-opus"
    card = _legacy_flagship_card()
    with kb.connect() as conn:
        with pytest.raises(kb.FlagshipAssigneeRefused, match="operator/orchestrator-only"):
            kb.authorize_flagship(conn, card, explicit, REASON)
        assert not kb._card_flagship_authorized(conn, card)
    assert _comments(card) == [] and _refused_at_dispatch(card)


def test_explicit_operator_actor_from_a_proven_process_is_kept(flagship_profiles, monkeypatch):
    """Positive control: a proven operator may name an operator label and the
    row is recorded under it."""
    from hermes_cli import kanban_identity, model_policy

    monkeypatch.setenv("HERMES_PROFILE", "apollo")
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.setattr(kanban_identity, "operator_label_proven", lambda _n: True)
    monkeypatch.setattr(kanban_identity, "worker_ancestor_profile", lambda: None)
    assert model_policy.flagship_actor("apollo") == "apollo"
    card = _legacy_flagship_card()
    with kb.connect() as conn:
        kb.authorize_flagship(conn, card, "apollo", REASON)
    _assert_authorized(card)


@pytest.mark.parametrize("proven", [False, True])
def test_override_body_write_gate_proves_the_claimed_author(flagship_profiles, monkeypatch, proven):
    """The write gate on ``add_comment`` (Prism eaff1b1213a3 / 8fb42c6aeb7e)
    judges the author the way ``flagship_actor`` does: a non-worker process
    that cannot prove the operator label it names is refused; the same call
    from a proven operator is written."""
    from hermes_cli import kanban_identity

    monkeypatch.setenv("HERMES_PROFILE", "daedalus-opus")
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.setattr(kanban_identity, "operator_label_proven", lambda _n: proven)
    monkeypatch.setattr(kanban_identity, "worker_ancestor_profile", lambda: None)
    card = _legacy_flagship_card()
    with kb.connect() as conn:
        if proven:
            kb.add_comment(conn, card, "apollo", OVERRIDE)
            assert kb._card_flagship_authorized(conn, card)
        else:
            with pytest.raises(kb.FlagshipAssigneeRefused, match="operator/orchestrator-only"):
                kb.add_comment(conn, card, "apollo", OVERRIDE)
            assert not kb._card_flagship_authorized(conn, card)
    if proven:
        _assert_authorized(card)
    else:
        assert _comments(card) == [] and _refused_at_dispatch(card)


# --- follow-ups t_b5dfd04f (Prism 40019830ac27, e25e5d877449, cd10822b8d6e) ---------


def _no_worker_card(assignee: str) -> str:
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="nw", assignee=None)
        conn.execute("UPDATE tasks SET assignee = ?, no_worker = 1 WHERE id = ?", (assignee, task_id))
        conn.commit()
    return task_id


def test_worker_ok_on_an_unchanged_flagship_assignee_is_gated_at_assign(flagship_profiles):
    """Clearing no_worker releases the card to dispatch on its profile route:
    a flagship default is refused here, not parked flagship_refused later."""
    task_id = _no_worker_card("daedalus-fable")
    with kb.connect() as conn:
        with pytest.raises(kb.FlagshipAssigneeRefused, match="assignee profile default"):
            kb.assign_task(conn, task_id, "daedalus-fable", worker_ok=True)
        assert kb.get_task(conn, task_id).no_worker
    assert _comments(task_id) == []
    plain = _no_worker_card("daedalus-opus")
    with kb.connect() as conn:
        assert kb.assign_task(conn, plain, "daedalus-opus", worker_ok=True)
        assert not kb.get_task(conn, plain).no_worker
    assert _comments(plain) == []


def test_batch_pin_clear_rechecks_the_route_inside_the_write_txn(flagship_profiles):
    """A reassign that lands between the CLI precheck and the batch commit
    (legal: a pinned card's route is its pin) cannot carry the clear onto a
    flagship profile default. The whole batch unwinds."""
    task_id = _card("daedalus-opus", "claude-opus-5-5")
    other = _card("daedalus-opus", "claude-opus-5-5")
    with kb.connect() as conn:
        assert kb.clear_pin_flagship_reason(conn, task_id, None) is None   # the precheck passes
        conn.execute("UPDATE tasks SET assignee = 'daedalus-fable' WHERE id = ?", (task_id,))
        conn.commit()
        with pytest.raises(kb.FlagshipAssigneeRefused, match="cannot clear the model pin"):
            kb.apply_batch_route_writes(conn, [
                kb.BatchRouteWrite(task_id=other, touch_model=True),
                kb.BatchRouteWrite(task_id=task_id, touch_model=True),
            ])
        assert kb.get_task(conn, task_id).model_override == "claude-opus-5-5"
        assert kb.get_task(conn, other).model_override == "claude-opus-5-5"
    assert _comments(task_id) == [] and _comments(other) == []


def test_reassign_worker_ok_reclaim_refuses_before_signalling(flagship_profiles, monkeypatch):
    """Prism 5df499a16242: reassign --worker-ok --reclaim on an unchanged
    flagship-default assignee is refused BEFORE the reclaim, so the live
    worker is never signalled and its claim stays intact."""
    reclaimed = []
    monkeypatch.setattr(kb, "reclaim_task", lambda *a, **k: reclaimed.append(a) or True)
    task_id = _card("daedalus-opus")
    with kb.connect() as conn:
        assert kb.claim_task(conn, task_id)
        conn.execute(
            "UPDATE tasks SET assignee = 'daedalus-fable', no_worker = 1 WHERE id = ?", (task_id,),
        )
        conn.commit()
        receipt: dict = {}
        with pytest.raises(kb.FlagshipAssigneeRefused, match="assignee profile default"):
            kb.reassign_task(conn, task_id, "daedalus-fable", reclaim_first=True,
                             worker_ok=True, receipt=receipt)
        assert reclaimed == [] and receipt == {}
        task = kb.get_task(conn, task_id)
        assert task.status == "running" and task.claim_lock and task.no_worker
    assert _comments(task_id) == []


def test_assign_reason_on_a_flagship_pin_is_recorded(flagship_profiles, proven_operator):
    """Prism 0cfef50d081b (#2425 r16): a card whose PIN itself is flagship (no
    alias remap) is a flagship route; an operator reason given on assign is
    recorded, not dropped, so the card is no longer refused at dispatch."""
    task_id = _legacy_flagship_card()
    with kb.connect() as conn:
        conn.execute("UPDATE tasks SET assignee = 'daedalus-opus', model_override = ? WHERE id = ?",
                     (FABLE, task_id))
        conn.commit()
    assert _refused_at_dispatch(task_id)
    with kb.connect() as conn:
        assert kb.assign_task(conn, task_id, "daedalus-opus", flagship_override_reason=REASON)
    _assert_authorized(task_id)


def test_assign_rejudges_the_route_inside_the_write_txn(flagship_profiles, monkeypatch):
    """Prism 5df499a16242 (carried, Stale Validation): the pre-txn check can
    pass on a pin that a concurrent clear removes before the assignment
    commits. The route is judged again on the locked row; the assignment
    unwinds instead of landing a flagship default with no override."""
    task_id = _card("daedalus-opus", "claude-opus-5-5")
    real = kb._assignee_flagship_reason
    calls = []

    def racing(conn, tid, *a, **k):
        calls.append(tid)
        out = real(conn, tid, *a, **k)
        if len(calls) == 1:   # the preflight passed; the pin is cleared before the txn
            conn.execute("UPDATE tasks SET model_override = NULL WHERE id = ?", (tid,))
            conn.commit()
        return out

    monkeypatch.setattr(kb, "_assignee_flagship_reason", racing)
    with kb.connect() as conn:
        with pytest.raises(kb.FlagshipAssigneeRefused, match="assignee profile default"):
            kb.assign_task(conn, task_id, "daedalus-fable")
        assert kb.get_task(conn, task_id).assignee == "daedalus-opus"
    assert len(calls) == 2
    assert _comments(task_id) == []


def _review_card(reviewer: str, implementer: str) -> str:
    """A ``review`` card parked on ``reviewer`` whose recorded implementer is ``implementer``."""
    task_id = _card(implementer)
    with kb.connect() as conn:
        run = kb.claim_task(conn, task_id, claimer=f"{implementer}:1")
        assert kb.request_review(conn, task_id, summary="ready", reviewer="human",
                                 expected_run_id=run.current_run_id) is True
        conn.execute("UPDATE tasks SET assignee = ? WHERE id = ?", (reviewer, task_id))
        conn.commit()
    return task_id


@pytest.mark.parametrize("verb", ["assign", "reassign"])
def test_request_changes_judges_the_post_send_back_route_before_committing(
    flagship_profiles, proven_operator, monkeypatch, verb,
):
    """Prism 8b7984d6e364: assigning a review card to its current reviewer with
    --request-changes looks unchanged, but the send-back restores the
    implementer first, so the card really moves implementer -> profile. That
    route is judged BEFORE the send-back commits: refused, still in review,
    hold intact, nothing written."""
    monkeypatch.setenv("HERMES_PROFILE", "apollo")
    task_id = _review_card("daedalus-fable", "daedalus-opus")
    fn = kb.assign_task if verb == "assign" else kb.reassign_task
    with kb.connect() as conn:
        with pytest.raises(kb.FlagshipAssigneeRefused, match="assignee profile default"):
            fn(conn, task_id, "daedalus-fable", request_changes_reason="rework", operator="apollo")
        task = kb.get_task(conn, task_id)
        assert (task.status, task.assignee) == ("review", "daedalus-fable")
        assert not conn.execute(
            "SELECT 1 FROM task_events WHERE task_id = ? AND kind = 'changes_requested'", (task_id,),
        ).fetchone()
    assert _comments(task_id) == []
    # The same send-back onto a non-flagship profile still lands.
    with kb.connect() as conn:
        assert fn(conn, task_id, "daedalus-sonnet", request_changes_reason="rework", operator="apollo")
        task = kb.get_task(conn, task_id)
        assert (task.status, task.assignee) == ("ready", "daedalus-sonnet")
