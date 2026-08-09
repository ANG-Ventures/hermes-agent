"""Tests for the cronjob tool schema shape.

Guards the description text that flags ``schedule`` (and ``prompt``) as
REQUIRED for ``action=create`` — the load-bearing fix for description-driven
models (e.g. Grok) that omit schedule when the schema only lists ``action``
in ``required[]``. See issue #32427 / PR #32448.
"""

from __future__ import annotations


def test_cronjob_schema_action_description_flags_create_requirements():
    """`action` description must state schedule + prompt are required for create."""
    from tools.cronjob_tools import CRONJOB_SCHEMA

    action_desc = CRONJOB_SCHEMA["parameters"]["properties"]["action"]["description"]
    assert "action=create" in action_desc
    assert "schedule" in action_desc
    assert "REQUIRED" in action_desc


def test_cronjob_schema_reasoning_effort_matches_generic_contract():
    from hermes_constants import VALID_REASONING_EFFORTS
    from tools.cronjob_tools import CRONJOB_SCHEMA

    enum = CRONJOB_SCHEMA["parameters"]["properties"]["reasoning_effort"]["enum"]
    assert enum == [*VALID_REASONING_EFFORTS, "none"]
    assert "max" in enum
    # 2026-07-15 parity merge: upstream #62650 made ultra a valid effort.
    assert "ultra" in enum
