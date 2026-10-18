"""Fork canary: the delegation fallback-chain contract (absent / list / [] / ``auto``) for subagents.

``delegation.fallback_providers: auto`` (hermes-agent#2043, t_3b6cc34b) lets a pinned child reuse the
parent's fallback lanes with its own model. Measured 2026-10-16: a pinned child (``delegation.model``
set) got NO fallback chain, so one lane outage killed every subagent. The resolver sits in
``tools/delegate_tool_config.py::_resolve_child_fallback_chain`` (+ ``hermes_cli/fallback_config.py``
``scoped_fallback_chain`` / ``remodel_chain_for_child``), a file upstream refactors (c47bf78d68). A
parity merge can drop the ``auto`` branch with every upstream test green; this canary goes red first.

Every case runs through the real spawn path (``_build_child_agent`` -> ``_resolve_child_runtime`` ->
``AIAgent(fallback_model=...)``, AIAgent mocked), not the helper alone. ``_load_config`` is patched to
an in-repo fixture shaped like the root config; no live config is read (D9).

  D1 unpinned child, ``fallback_providers`` absent -> parent chain verbatim (upstream behaviour).
  D2 pinned child, absent -> None (pinned children borrow nothing; the 2026-10-16 gap).
  D3 pinned child, literal list -> that list.
  D4 ``[]`` -> None, pinned or not.
  D5 pinned child + ``auto`` -> parent lanes re-aimed at the child's model: same-vendor rungs only,
     cross-vendor terminal rung untouched, the child's own route dropped, order kept, deduped.
  D6 unpinned child + ``auto`` -> parent chain verbatim.
  D7 ``auto`` with an empty parent chain -> None + exactly one WARNING.
  D8 config validator: null / list / [] / 'auto' / ' AUTO ' accepted; 'inherit' / 'parent' / 1 /
     True / dict refused, the error names 'auto' and never echoes the value.
  D9 fixture, not ``~/.hermes/config.yaml``: ``ROOT_PARENT_CHAIN`` mirrors the root shape.

Registered in ``docs/sync/fork-features.json`` ("delegation fallback_providers: auto / pinned-child
chain contract") so the parity-merge checklist runs it.
"""

from __future__ import annotations

import copy
import logging
from unittest.mock import MagicMock, patch

import pytest

from tests.tools.test_delegate import _make_mock_parent
from tools.delegate_tool import _build_child_agent
from hermes_cli.config import validate_config_structure

PARENT_PROVIDER, PARENT_MODEL = "claude-alr", "claude-fable-5-1"
CHILD_MODEL = "claude-opus-5-5"

# D9: root config shape (fable on dtlr/btpr/bpr lanes -> cpa sol), plus the two edges D5 needs:
# a rung that becomes the child's own route after remodel, and a rung that collapses onto another.
ROOT_PARENT_CHAIN = [
    {"provider": "claude-dtlr", "model": "claude-fable-5-1"},
    {"provider": "claude-btpr", "model": "claude-fable-5-1", "base_url": "http://btpr.example/v1",
     "key_env": "BTPR_KEY"},
    {"provider": "claude-alr", "model": "claude-fable-5-1"},   # -> claude-alr/opus = child primary: dropped
    {"provider": "claude-bpr", "model": "claude-fable-5-1"},
    {"provider": "claude-bpr", "model": "claude-sonnet-5-5"},  # -> claude-bpr/opus again: deduped
    {"provider": "cpa", "model": "gpt-6.1-sol"},               # cross-vendor terminal rung: untouched
]
DECLARED = [{"provider": "deepseek", "model": "deepseek-chat"}]
ABSENT = object()


def _parent(chain):
    parent = _make_mock_parent(depth=0)
    parent.provider, parent.requested_provider, parent.model = PARENT_PROVIDER, PARENT_PROVIDER, PARENT_MODEL
    parent._fallback_chain = copy.deepcopy(chain)
    return parent


def _child_chain(declared=ABSENT, *, pinned, parent_chain=ROOT_PARENT_CHAIN):
    cfg = {} if declared is ABSENT else {"fallback_providers": copy.deepcopy(declared)}
    parent = _parent(parent_chain)
    with patch("tools.delegate_tool._load_config", return_value=cfg), patch("run_agent.AIAgent") as agent:
        agent.return_value = MagicMock()
        _build_child_agent(task_index=0, goal="fallback canary", context=None, toolsets=None,
                           model=CHILD_MODEL if pinned else None, max_iterations=5,
                           parent_agent=parent, task_count=1)
    kwargs = agent.call_args.kwargs
    assert kwargs["model"] == (CHILD_MODEL if pinned else PARENT_MODEL)
    assert parent._fallback_chain == parent_chain  # resolved per spawn; parent state untouched
    return kwargs["fallback_model"]


def _routes(chain):
    return [(r["provider"], r["model"]) for r in chain]


def test_d1_unpinned_absent_inherits_parent_chain_verbatim():
    assert _child_chain(pinned=False) == ROOT_PARENT_CHAIN


def test_d2_pinned_absent_has_no_chain():
    assert _child_chain(pinned=True) is None


def test_d3_pinned_literal_list_is_used():
    assert _child_chain(DECLARED, pinned=True) == DECLARED


@pytest.mark.parametrize("pinned", [True, False])
def test_d4_empty_list_disables_fallback(pinned):
    assert _child_chain([], pinned=pinned) is None


@pytest.mark.parametrize("spelling", ["auto", " AUTO "])
def test_d5_pinned_auto_reaims_parent_lanes_at_child_model(spelling):
    chain = _child_chain(spelling, pinned=True)
    assert _routes(chain) == [
        ("claude-dtlr", CHILD_MODEL),
        ("claude-btpr", CHILD_MODEL),
        ("claude-bpr", CHILD_MODEL),
        ("cpa", "gpt-6.1-sol"),
    ]
    # The lane (endpoint, credential pointer) survives the remodel; only the model moves.
    assert chain[1] == {"provider": "claude-btpr", "model": CHILD_MODEL,
                        "base_url": "http://btpr.example/v1", "key_env": "BTPR_KEY"}


def test_d6_unpinned_auto_inherits_parent_chain_verbatim():
    assert _child_chain("auto", pinned=False) == ROOT_PARENT_CHAIN


@pytest.mark.parametrize("pinned", [True, False])
def test_d7_auto_with_empty_parent_chain_is_none_with_one_warning(pinned, caplog):
    with caplog.at_level(logging.WARNING):
        assert _child_chain("auto", pinned=pinned, parent_chain=[]) is None
    warnings = [r for r in caplog.records
                if r.levelno == logging.WARNING and "'auto'" in r.getMessage()]
    assert len(warnings) == 1


def _fallback_issues(value):
    return [i for i in validate_config_structure({"delegation": {"fallback_providers": value}})
            if "delegation.fallback_providers" in i.message]


@pytest.mark.parametrize("value", [None, [], DECLARED, "auto", " AUTO "])
def test_d8_validator_accepts_null_list_empty_and_auto(value):
    assert _fallback_issues(value) == []


@pytest.mark.parametrize("value", [
    "inherit", "parent", 1, True,
    {"provider": "custom", "model": "m", "api_key": "CANARY_SECRET_MUST_NOT_ECHO"},
])
def test_d8_validator_refuses_other_shapes_without_echoing_them(value):
    issues = _fallback_issues(value)
    assert len(issues) == 1 and issues[0].severity == "error"
    assert "'auto'" in issues[0].message
    text = issues[0].message + issues[0].hint
    assert "CANARY_SECRET_MUST_NOT_ECHO" not in text and repr(value) not in text
