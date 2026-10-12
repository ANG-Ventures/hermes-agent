"""t_7de359d9: the two 2026-10-12 notify-suppress misses, as predicate tests.

Miss 1 (13:45, t_69572750): kanban-review-merge-pass closed the card with no actor_profile
stamp; the prose fallback read the merge-pass RULING's first tag "(CI gate, …)" as the
closer -> other-actor -> delivered. Fix: the closer stamps actor_profile (``kanban complete
--actor``); the fallback stays, but logs.

Miss 2 (14:10, t_fb95da9b): review-card closer, identity parsed correctly, but the card had
no review_coverage APPROVE (the PR landed under the lander before the attended read) ->
closer-without-operator-approve -> delivered. Fix: a trusted closer on a card the receiver
OWNS is self-operator by construction; the APPROVE gate applies to foreign cards only.
"""
from __future__ import annotations

from types import SimpleNamespace

from gateway import kanban_notify_suppress as ns

HOME_SID = "20261008_184838_dbe0955f"

# scripts/kanban-review-merge-pass.py RULING, verbatim from task_events 657420.
MERGE_PASS_SUMMARY = ("ANG-Ventures/hermes-home#3925 @ 88b9dd9b3 merged as 3352abe62 2026-10-12T20:34:38Z; "
                      "CI 10/12; landed by kanban-review-merge-pass (CI gate, Ace 09-24 13:08). "
                      "Deploy: pending (home-reconcile-origin / Apollo).")


def _ev(**row):
    base = {"id": 657420, "task_id": "t_69572750", "kind": "completed", "created_at": 1791837897,
            "actor_profile": None, "actor_session_id": None, "payload": {"summary": MERGE_PASS_SUMMARY}}
    base.update(row)
    return SimpleNamespace(**base)


def test_stamped_merge_pass_closer_is_redundant_after_operator_approve(caplog):
    cfg = ns.parse_config({})
    ev = _ev(actor_profile="kanban-review-merge-pass")
    card = {"session_id": "20261012_other_home", "operator_profile": "default",
            "latest_approve": {"by": "apollo", "ts": 1791837897 - 5 * 60}}
    with caplog.at_level("INFO", logger="gateway.kanban_notify_suppress"):
        assert ns.is_redundant(ev, card, {"session_ids": {HOME_SID}}, cfg) == (True, "closer-after-operator-approve")
    assert "from summary prose" not in caplog.text


def test_unstamped_merge_pass_summary_falls_back_loudly(caplog):
    """The prose fallback is a LAST resort and announces itself in gateway.log. The RULING's
    first tag is 'ci gate': no closer list matches it (the regex is not widened, by design)."""
    with caplog.at_level("INFO", logger="gateway.kanban_notify_suppress"):
        assert ns.closer_identity(_ev()) == "ci gate"
    assert "closer identity from summary prose: 'ci gate'" in caplog.text
    assert not ns.is_closer("ci gate", ns.DEFAULT_CLOSERS)


def test_unstamped_merge_pass_close_of_owned_card_is_redundant():
    """Miss 1 as observed (no stamp) AND an APPROVE in window: still suppressed, because the
    receiver owns the card -- but only once the closer is recognisable. Unstamped + unowned
    stays delivered (fail open), which is why the stamp is the mechanism."""
    cfg = ns.parse_config({})
    approve = {"by": "apollo", "ts": 1791837897 - 5 * 60}
    owned = {"session_id": HOME_SID, "operator_profile": "default", "latest_approve": approve}
    stamped = _ev(actor_profile="kanban-review-merge-pass")
    assert ns.is_redundant(stamped, owned, {"session_ids": {HOME_SID}}, cfg) == (True, "closer-on-owned-card")
    assert ns.is_redundant(_ev(), owned, {"session_ids": {HOME_SID}}, cfg) == (False, "other-actor")


def test_trusted_closer_on_owned_card_needs_no_approve():
    """Miss 2: review-card closer, identity correct, home session IS the subscriber's."""
    cfg = ns.parse_config({})
    ev = _ev(id=1, task_id="t_fb95da9b", created_at=1791839400, actor_profile="review-card closer",
             payload={"summary": "Apollo merge pass (review-card closer, board default): vh#3852 landed"})
    card = {"session_id": HOME_SID, "operator_profile": "default", "latest_approve": None}
    # owner-wake shape: the receiver's own session ids
    assert ns.is_redundant(ev, card, {"session_ids": {HOME_SID}}, cfg) == (True, "closer-on-owned-card")
    # notifier shape: the subscription is the home chat
    assert ns.is_redundant(ev, card, {"self_event_ids": set(), "is_home": True}, cfg) == (True, "closer-on-owned-card")
    # the same close reaching a FOREIGN subscriber still gets its line
    assert ns.is_redundant(ev, card, {"self_event_ids": set(), "is_home": False}, cfg) == (False, "foreign-subscriber")


def test_trusted_closer_on_foreign_card_still_needs_approve():
    cfg = ns.parse_config({})
    ev = _ev(id=2, task_id="t_other", created_at=1791839400, actor_profile="review-card closer", payload={})
    card = {"session_id": "20261012_000000_someone_else", "operator_profile": "default", "latest_approve": None}
    assert ns.is_redundant(ev, card, {"session_ids": {HOME_SID}}, cfg) == (False, "closer-without-operator-approve")
    card["latest_approve"] = {"by": "apollo", "ts": 1791839400 - 60}
    assert ns.is_redundant(ev, card, {"session_ids": {HOME_SID}}, cfg) == (True, "closer-after-operator-approve")


def test_owned_card_shortcut_requires_a_stamped_exact_closer():
    """Prism #1933 r1: the owned shortcut has no APPROVE and no window, so it trusts only a
    STAMPED actor_profile that is EXACTLY a configured closer. An unstamped summary whose first
    tag is a closer name, or a stamped prefix variant ("merge-pass follow-up"), keeps the
    APPROVE-gated path and is delivered without one."""
    cfg = ns.parse_config({})
    owned = {"session_id": HOME_SID, "operator_profile": "default", "latest_approve": None}
    sub = {"session_ids": {HOME_SID}}
    prose = _ev(id=4, task_id="t_fb95da9b", created_at=1791839400, actor_profile=None,
                payload={"summary": "(merge-pass follow-up) tidied the card"})
    assert ns.closer_identity(prose) == "merge-pass follow-up"
    assert ns.is_closer("merge-pass follow-up", cfg.closers)   # prefix match, by design for the APPROVE path
    assert ns.is_redundant(prose, owned, sub, cfg) == (False, "closer-without-operator-approve")
    prefix = _ev(id=5, task_id="t_fb95da9b", created_at=1791839400, actor_profile="merge-pass follow-up", payload={})
    assert ns.is_redundant(prefix, owned, sub, cfg) == (False, "closer-without-operator-approve")
    exact = _ev(id=6, task_id="t_fb95da9b", created_at=1791839400, actor_profile="Merge-Pass", payload={})
    assert ns.is_redundant(exact, owned, sub, cfg) == (True, "closer-on-owned-card")


def test_ownership_alone_never_suppresses_a_non_closer():
    cfg = ns.parse_config({})
    ev = _ev(id=3, task_id="t_fb95da9b", created_at=1791839400, actor_profile="daedalus-opus", payload={})
    owned = {"session_id": HOME_SID, "operator_profile": "default", "latest_approve": None}
    assert ns.is_redundant(ev, owned, {"session_ids": {HOME_SID}}, cfg) == (False, "other-actor")
