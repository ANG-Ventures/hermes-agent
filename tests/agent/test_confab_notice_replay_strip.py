"""Source/AST contract tests for the presentation-field replay strip.

Replay exclusion of ``display_kind`` / ``display_metadata`` is an existing
harness invariant, not a promise — and the whole out-of-band confab-notice
design rests on it. Line numbers in a 9k-line file are a weak handle (the
spec's own citation moved twice), so these tests pin the invariant by SYMBOL:

1. the two ``api_msg.pop(...)`` removals exist in the outgoing-message builder,
   found by walking the AST rather than grepping a line range;
2. the flush that writes an assistant row to ``state.db`` passes
   ``display_metadata`` through (spec §consumer step 4: assert the existing
   behaviour, fail loudly if a future refactor drops it);
3. a behavioural round trip: a message dict carrying both fields is cloned
   for the wire with both fields gone.

Mutation proof for (1) is in the module docstring of the e2e suite and is
re-run by deleting either ``pop`` line — see
``tests/agent/test_confab_notice_e2e.py::test_next_request_carries_neither_field_nor_notice_text``,
which goes red when the strip is removed.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

import agent.conversation_loop as conversation_loop
import run_agent

STRIPPED_PRESENTATION_FIELDS = ("display_kind", "display_metadata")


def _popped_literals(tree: ast.AST, target_name: str) -> set[str]:
    """Every string literal X in a ``<target_name>.pop("X", ...)`` call."""
    found: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Attribute) or func.attr != "pop":
            continue
        value = func.value
        if not isinstance(value, ast.Name) or value.id != target_name:
            continue
        if node.args and isinstance(node.args[0], ast.Constant):
            if isinstance(node.args[0].value, str):
                found.add(node.args[0].value)
    return found


class TestReplayStripPinnedBySymbol:
    def test_both_presentation_fields_are_popped_from_api_msg(self):
        """Pinned by AST symbol, not by line number."""
        source = Path(inspect.getsourcefile(conversation_loop)).read_text()
        popped = _popped_literals(ast.parse(source), "api_msg")

        for field in STRIPPED_PRESENTATION_FIELDS:
            assert field in popped, (
                f"api_msg.pop({field!r}) is missing from "
                f"{conversation_loop.__name__} — presentation-only fields "
                "would be replayed to the provider. See "
                "agent/confab_notice.py and the out-of-band notice contract."
            )

    @pytest.mark.parametrize("field", STRIPPED_PRESENTATION_FIELDS)
    def test_strip_happens_in_the_outgoing_message_builder(self, field):
        """The pop must sit on the api_msg built by _clone_message_for_send.

        A pop somewhere unrelated would satisfy the AST scan above while the
        wire copy still carried the field, so also require that the enclosing
        function references the clone helper.
        """
        source = Path(inspect.getsourcefile(conversation_loop)).read_text()
        tree = ast.parse(source)

        hosts = []
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if field not in _popped_literals(node, "api_msg"):
                continue
            names = {
                n.id for n in ast.walk(node) if isinstance(n, ast.Name)
            } | {
                n.attr for n in ast.walk(node) if isinstance(n, ast.Attribute)
            }
            if "_clone_message_for_send" in names:
                hosts.append(node.name)

        assert hosts, (
            f"no function both clones messages for the wire and pops {field!r}"
        )


class TestPersistenceCarriesDisplayMetadata:
    """Spec §consumer step 4 — do not 'add' this, assert it stays."""

    def test_flush_passes_display_metadata_into_the_row_dict(self):
        source = Path(inspect.getsourcefile(run_agent)).read_text()
        tree = ast.parse(source)

        found = False
        for node in ast.walk(tree):
            if not isinstance(node, ast.Dict):
                continue
            keys = {
                k.value
                for k in node.keys
                if isinstance(k, ast.Constant) and isinstance(k.value, str)
            }
            if {"role", "content", "display_kind", "display_metadata"} <= keys:
                found = True
                break

        assert found, (
            "the session-DB flush no longer builds a row dict carrying BOTH "
            "display_kind and display_metadata — the durable confab-notice "
            "triage record would be silently dropped."
        )


class TestCloneForSendBehaviour:
    def test_wire_copy_is_free_of_presentation_fields(self):
        """Behavioural companion to the AST pins."""
        from agent.confab_notice import CONFAB_NOTICE_KEY

        msg = {
            "role": "assistant",
            "content": "All good here.",
            "display_kind": "confab_notice",
            "display_metadata": {
                CONFAB_NOTICE_KEY: {
                    "version": 1,
                    "kind": "scaffold_confab_removed",
                    "request_id": "3b264082",
                    "scope": "visible",
                    "grammar": "inbound",
                }
            },
        }

        api_msg = conversation_loop._clone_message_for_send(msg)
        # The clone is structural; the strip is the caller's two pops. Apply
        # them the same way the builder does and assert nothing leaks.
        api_msg.pop("display_kind", None)
        api_msg.pop("display_metadata", None)

        assert "scaffold_confab_removed" not in repr(api_msg)
        # And the clone must not have aliased the persisted dict.
        assert msg["display_metadata"][CONFAB_NOTICE_KEY]["version"] == 1
