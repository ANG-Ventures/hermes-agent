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


# parity 2026-08-30: test_cronjob_schema_reasoning_effort_matches_generic_contract
# removed — upstream 991af03f4c (2026-08-20) deliberately took reasoning_effort OFF
# the model-facing cronjob schema (policy: models never choose model config; the
# per-job pin lives in `hermes cron create/edit --reasoning-effort`). The absence is
# pinned by tests/cron/test_cron_reasoning_effort.py::test_schema_does_not_expose_reasoning_effort.
