"""Contract tests for the confab-notice fixes FleetReview flagged on PR #764.

Five P1 defects, each pinned here by the behaviour that was wrong:

1. **Duplicate Accepted** — the streaming accumulator kept the FIRST of two
   valid notices. Two catch records for one response means we cannot know
   which describes the reply, so the contract is fail-closed: mark the
   accumulator invalid and forward NO notice.
2. **Alert Suppression** — the announce ledger was scoped to the agent's
   LIFETIME and keyed only on the provider's ``request_id``. A restarted ID
   sequence or a plain collision silently suppressed a later turn's genuine
   warning — the exact signal this feature exists to deliver.
3. **False Notice** — readers presented "confabulation caught" off the
   open-ended ``display_kind`` string alone, so a reloaded user/system row
   from imported or malformed history claimed a confirmed catch.
5. **Provenance** — what the transport does and does NOT enforce about WHERE a
   schema-valid notice came from. See ``TestProvenance`` for the ruling.

The streaming-transport end-to-end path is covered in
``test_confab_notice_streaming_e2e.py``.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from agent.confab_notice import (
    CONFAB_NOTICE_DISPLAY_KIND,
    CONFAB_NOTICE_FIELD,
    CONFAB_NOTICE_KEY,
    notice_from_display_row,
    should_announce_notice,
)

VALID = {
    "version": 1,
    "kind": "scaffold_confab_removed",
    "request_id": "3b264082",
    "scope": "visible",
    "grammar": "inbound",
}


class _Agent:
    """Bare attribute bag — the ledger lives on the agent object."""


class TestAnnounceLedgerIsTurnScoped:
    """P1 Alert Suppression — agent/conversation_loop.py:7670."""

    def test_announces_once_within_one_turn(self):
        """A retry/failover re-normalizing the same response must not double-announce."""
        agent = _Agent()

        assert should_announce_notice(agent, dict(VALID), "turn-1") is True
        assert should_announce_notice(agent, dict(VALID), "turn-1") is False
        assert should_announce_notice(agent, dict(VALID), "turn-1") is False

    def test_same_request_id_on_a_later_turn_is_NOT_suppressed(self):
        """THE defect: a bridge restarting its ID sequence, or an ID collision.

        Under the lifetime-scoped ledger this returned False and the user was
        never told about a genuine, confirmed confabulation catch.
        """
        agent = _Agent()

        assert should_announce_notice(agent, dict(VALID), "turn-1") is True
        assert should_announce_notice(agent, dict(VALID), "turn-2") is True
        assert should_announce_notice(agent, dict(VALID), "turn-3") is True

    def test_two_providers_colliding_on_one_id_in_one_turn_still_dedups(self):
        """Within a turn the dedup is still by request_id — that is its job."""
        agent = _Agent()

        assert should_announce_notice(agent, {**VALID, "request_id": "dup"}, "t") is True
        assert should_announce_notice(agent, {**VALID, "request_id": "dup"}, "t") is False
        assert should_announce_notice(agent, {**VALID, "request_id": "other"}, "t") is True

    def test_ledger_is_evicted_on_turn_change_and_does_not_grow(self):
        """Scoping without eviction would still leak keys and grow unbounded."""
        agent = _Agent()

        for turn in range(50):
            should_announce_notice(agent, {**VALID, "request_id": f"r{turn}"}, f"turn-{turn}")

        ledger = agent._confab_notices_announced
        assert ledger["turn_id"] == "turn-49"
        assert len(ledger["keys"]) == 1, "previous turns' keys were not evicted"

    def test_a_legacy_set_shaped_ledger_is_repaired_not_crashed(self):
        """An older build left a bare set on the agent; do not explode on it."""
        agent = _Agent()
        agent._confab_notices_announced = {"3b264082"}

        assert should_announce_notice(agent, dict(VALID), "turn-1") is True

    def test_non_dict_notice_never_announces(self):
        assert should_announce_notice(_Agent(), None, "t") is False
        assert should_announce_notice(_Agent(), "scaffold_confab_removed", "t") is False


class TestDisplayRowGate:
    """P1 False Notice — hermes_cli/cli_agent_setup_mixin.py:801."""

    def _row(self, role="assistant", kind=CONFAB_NOTICE_DISPLAY_KIND, meta=None):
        if meta is None:
            meta = {CONFAB_NOTICE_KEY: dict(VALID)}
        return role, kind, meta

    def test_a_real_assistant_notice_row_qualifies(self):
        assert notice_from_display_row(*self._row()) == VALID

    @pytest.mark.parametrize("role", ["user", "system", "tool", None, ""])
    def test_non_assistant_rows_never_claim_a_catch(self, role):
        """THE defect: imported/malformed history tagged on a non-model turn."""
        assert notice_from_display_row(*self._row(role=role)) is None

    def test_row_without_the_metadata_is_rejected(self):
        """display_kind alone is an open string — it is not evidence."""
        assert notice_from_display_row(*self._row(meta={})) is None

    @pytest.mark.parametrize(
        "meta",
        [
            {CONFAB_NOTICE_KEY: {"version": 99, "kind": "scaffold_confab_removed",
                                 "request_id": "x", "scope": "visible", "grammar": None}},
            {CONFAB_NOTICE_KEY: {**VALID, "kind": "totally_made_up"}},
            {CONFAB_NOTICE_KEY: {**VALID, "request_id": ""}},
            {CONFAB_NOTICE_KEY: {**VALID, "scope": "everything"}},
            {CONFAB_NOTICE_KEY: "scaffold_confab_removed"},
            {CONFAB_NOTICE_KEY: None},
            {"something_else": dict(VALID)},
            "not json at all",
            b"\x00\x01",
            123,
            None,
            [],
        ],
    )
    def test_metadata_must_re_validate_against_the_v1_schema(self, meta):
        assert notice_from_display_row("assistant", CONFAB_NOTICE_DISPLAY_KIND, meta) is None

    def test_a_json_serialized_metadata_column_still_validates(self):
        """Stores that serialize the column must not lose the row's meaning."""
        raw = json.dumps({CONFAB_NOTICE_KEY: VALID})
        assert notice_from_display_row("assistant", CONFAB_NOTICE_DISPLAY_KIND, raw) == VALID

    def test_other_display_kinds_are_not_claimed(self):
        assert notice_from_display_row("assistant", "model_switch", {CONFAB_NOTICE_KEY: dict(VALID)}) is None
        assert notice_from_display_row("assistant", None, {CONFAB_NOTICE_KEY: dict(VALID)}) is None

    def test_returns_the_notice_so_callers_can_key_off_kind(self):
        """Future kinds must be distinguishable — not all labelled identically."""
        out = notice_from_display_row(*self._row())
        assert out["kind"] == "scaffold_confab_removed"
        assert out["request_id"] == "3b264082"


class TestCLIRecapGating:
    """The CLI resume recap is the surface the False Notice P1 was filed on."""

    def _recap_events(self, history):
        """Drive the real gate the recap uses over a history list."""
        events = []
        for msg in history:
            if msg.get("display_kind") == CONFAB_NOTICE_DISPLAY_KIND:
                if notice_from_display_row(
                    msg.get("role"), msg.get("display_kind"), msg.get("display_metadata")
                ):
                    events.append("confabulation caught — scaffold text removed")
        return events

    def test_reloaded_user_row_tagged_confab_notice_shows_no_claim(self):
        history = [
            {"role": "user", "content": "hi",
             "display_kind": CONFAB_NOTICE_DISPLAY_KIND,
             "display_metadata": {CONFAB_NOTICE_KEY: dict(VALID)}},
        ]
        assert self._recap_events(history) == []

    def test_genuine_assistant_row_still_shows_the_claim(self):
        history = [
            {"role": "assistant", "content": "All good here.",
             "display_kind": CONFAB_NOTICE_DISPLAY_KIND,
             "display_metadata": {CONFAB_NOTICE_KEY: dict(VALID)}},
        ]
        assert len(self._recap_events(history)) == 1


class TestProvenance:
    """P1 Provenance Untested — tests/agent/test_confab_notice.py:201.

    RULING, stated so it cannot be silently re-litigated: **provenance is NOT
    enforced.** ``validate_confab_notice`` gates the SCHEMA; nothing in the
    consumer inspects which provider or base_url the response came from, so a
    non-BPX provider emitting a schema-valid notice IS accepted.

    That is deliberate and safe — the notice carries no model-visible text and
    no authority; it is a presentation-only, replay-stripped triage tag, and
    the worst a hostile provider achieves is labelling its OWN reply as
    caught. Enforcing provenance would instead break every legitimate proxy in
    front of the bridge.

    The original test claimed to cover "a non-bpx provider" while actually
    only rejecting an invalid ``kind`` — it proved nothing about provenance.
    These tests state the real behaviour, so a future reader is not misled
    into believing a guard exists.
    """

    @pytest.fixture
    def transport(self):
        import agent.transports.chat_completions  # noqa: F401
        from agent.transports import get_transport

        return get_transport("chat_completions")

    def _response(self, notice, model):
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content="All good here.", tool_calls=None),
                    finish_reason="stop",
                )
            ],
            usage=None,
            model=model,
            **{CONFAB_NOTICE_FIELD: notice},
        )

    @pytest.mark.parametrize(
        "model", ["gpt-5.6-sol", "gemini-3-pro", "some-random-proxy", "claude-bpx"]
    )
    def test_a_schema_valid_notice_is_accepted_regardless_of_provenance(
        self, transport, model
    ):
        """Explicit non-BPX provenance, schema-VALID notice → accepted.

        This is the case the old test claimed to cover and did not. It passes
        by design: see the class docstring. If provenance enforcement is ever
        added, THIS test is the one that must be changed, deliberately.
        """
        normalized = transport.normalize_response(self._response(dict(VALID), model))
        assert normalized.confab_notice == VALID

    @pytest.mark.parametrize(
        "model", ["gpt-5.6-sol", "gemini-3-pro", "some-random-proxy", "claude-bpx"]
    )
    def test_schema_invalid_notices_are_rejected_regardless_of_provenance(
        self, transport, model
    ):
        """The gate that DOES exist holds for every provider identity."""
        bad = {**VALID, "kind": "totally_made_up"}
        normalized = transport.normalize_response(self._response(bad, model))
        assert normalized.confab_notice is None

    def test_an_accepted_notice_carries_no_model_visible_text(self, transport):
        """Why accepting it is safe: the payload is labels, not prose."""
        normalized = transport.normalize_response(
            self._response(dict(VALID), "some-random-proxy")
        )
        assert normalized.content == "All good here."
        assert set(normalized.confab_notice) == {
            "version", "kind", "request_id", "scope", "grammar"
        }
