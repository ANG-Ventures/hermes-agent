"""S5 actionable-only wakes (t_4f6afb67): the pure predicates and the ``kanban show`` section.

The end-to-end turn counts live in the wake-contract canary (W10-W14,
tests/fork_canaries/test_fork_canary_kanban_wake_contract.py). These pin the
relations the canary relies on: what makes two reds the same news, which
transitions are the home session's own echo, which send-backs are a closer's
auto-route, and that every withheld event reaches ``kanban show``.
"""
from __future__ import annotations

from types import SimpleNamespace as NS

from gateway import kanban_notify_suppress as ns
from gateway import kanban_owner_wake as ow
from hermes_cli import kanban_show_suppressed as kss

H = {"state": "open", "mergeable_state": "clean", "failing": ["lint", "ci"], "head": "a" * 40}


def test_red_key_names_card_pr_head_and_red_set_order_free():
    k = ow.red_key("t_1", "o/r#1", H)
    assert k == ow.red_key("t_1", "o/r#1", dict(H, failing=["ci", "lint"]))
    for other in (ow.red_key("t_2", "o/r#1", H), ow.red_key("t_1", "o/r#2", H),
                  ow.red_key("t_1", "o/r#1", dict(H, head="b" * 40)),
                  ow.red_key("t_1", "o/r#1", dict(H, failing=["lint"])),
                  ow.red_key("t_1", "o/r#1", dict(H, mergeable_state="dirty"))):
        assert other != k


def test_red_noise_order_stale_then_duplicate():
    seen = {"k": 1.0}
    assert ns.red_handback_noise(["k"], seen, "ready") == ns.REASON_STALE_HANDBACK
    assert ns.red_handback_noise(["k"], seen, "review") == ns.REASON_DUPLICATE_RED
    assert ns.red_handback_noise(["k", "new"], seen, "review") is None, "one new red is news"
    assert ns.red_handback_noise(["k"], {}, None) is None


def test_red_seen_prunes_by_ttl():
    seen = {"old": 0.0}
    ns.note_red_seen(seen, ["new"], ns.RED_SEEN_TTL_S + 1)
    assert seen == {"new": ns.RED_SEEN_TTL_S + 1}


def test_own_echo_only_for_the_home_session_in_the_home_chat():
    ev = {"kind": "changes_requested", "actor_session_id": "home"}
    assert ns.is_own_echo(ev, "home", True)
    assert not ns.is_own_echo(ev, "home", False), "a foreign subscriber still hears it"
    assert not ns.is_own_echo(dict(ev, actor_session_id="worker"), "home", True)
    assert not ns.is_own_echo(dict(ev, kind="completed"), "home", True), "completed is notify_suppress' job"
    assert not ns.is_own_echo(dict(ev, actor_session_id=None), "home", True), "no actor fails open"


def test_auto_route_trusts_only_a_stamped_closer():
    cfg = ns.SuppressConfig()
    ev = {"kind": "changes_requested", "actor_profile": "kanban-review-merge-pass"}
    assert ns.is_auto_route(ev, cfg)
    assert not ns.is_auto_route(dict(ev, actor_profile="default"), cfg), "the operator's own send-back"
    assert not ns.is_auto_route(dict(ev, actor_profile="merge-pass follow-up"), cfg), "no prefix match"
    assert not ns.is_auto_route(dict(ev, kind="completed"), cfg)


def test_actionable_only_defaults_on_and_has_a_kill_switch():
    assert ns.parse_config({}).actionable_only is True
    assert ns.parse_config({"notify_suppress": {"actionable_only": False}}).actionable_only is False


def test_kanban_show_lists_every_withheld_event():
    events = [NS(kind="notify_suppressed", created_at=10,
                 payload={"event_id": 7, "kind": "review_requested", "reason": "inherited-red",
                          "path": "owner-wake", "actor": "worker"}),
              NS(kind="heartbeat", created_at=11, payload={})]
    rows = kss.suppressed_rows(events)
    assert [(r["event_id"], r["reason"], r["path"]) for r in rows] == [(7, "inherited-red", "owner-wake")]
    line, = kss.format_rows(rows, str)
    assert "#7" in line and "inherited-red" in line and "owner-wake" in line
