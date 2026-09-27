"""Wire contract: cache_control markers land ONLY at real breakpoint positions.

Fleet floor (t_dac7e751, rule from t_c271a998): Anthropic honours a prompt-cache
breakpoint only on a block in ``system[i]``, ``tools[i]``,
``messages[i].content[j]`` or a tool_result's nested ``content[k]``. A
``cache_control`` key anywhere else in the request body is caller data (a tool
schema property, a historical tool_use input) and must reach the wire
byte-exact -- never stamped, never counted.

The test drives the real path end to end: ``build_prompt_cache_plan`` (the
harness breakpoint builder) -> ``build_anthropic_kwargs`` (the native wire
adapter), then walks every ``cache_control`` key in the resulting request.
It asserts:

* every marker the harness placed sits at a breakpoint position;
* every such marker carries the configured tier (``ttl: 1h``);
* the breakpoint budget (4) is respected;
* ``cache_control`` keys that are caller data are unchanged.
"""

from __future__ import annotations

import copy
import json
import re

import pytest

from agent.anthropic_adapter import build_anthropic_kwargs
from agent.prompt_caching import build_prompt_cache_plan

MAX_BREAKPOINTS = 4

# Paths are tuples of dict keys / list indexes from the request root.
_BREAKPOINT_PATHS = (
    re.compile(r"^system/\d+$"),
    re.compile(r"^tools/\d+$"),
    re.compile(r"^messages/\d+/content/\d+$"),
    re.compile(r"^messages/\d+/content/\d+/content/\d+$"),
)

# Caller data that happens to use the key name. It must survive untouched.
_SCHEMA_DECOY = {"type": "string", "description": "caller field named cache_control"}
_TOOL_INPUT_DECOY = {"type": "ephemeral"}

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "set_cache",
            "description": "Tool whose schema has a property named cache_control.",
            "parameters": {
                "type": "object",
                "properties": {"cache_control": copy.deepcopy(_SCHEMA_DECOY)},
                "required": ["cache_control"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a file.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        },
    },
]


def _transcript() -> list:
    return [
        {"role": "system", "content": "STATIC PREFIX\nvolatile suffix 2026-09-25"},
        {"role": "user", "content": "set the cache field"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {
                        "name": "set_cache",
                        "arguments": json.dumps({"cache_control": _TOOL_INPUT_DECOY}),
                    },
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call_1", "content": "ok"},
        {"role": "assistant", "content": "done"},
        {"role": "user", "content": "now read a file"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call_2",
                    "type": "function",
                    "function": {"name": "read_file", "arguments": '{"path": "a.txt"}'},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call_2", "content": "file body"},
        {"role": "user", "content": "thanks"},
    ]


def _walk_cache_controls(obj, path=()):
    """Yield (path, value) for every ``cache_control`` key in ``obj``."""
    if isinstance(obj, dict):
        for key, value in obj.items():
            if key == "cache_control":
                yield path, value
            yield from _walk_cache_controls(value, path + (key,))
    elif isinstance(obj, list):
        for index, value in enumerate(obj):
            yield from _walk_cache_controls(value, path + (index,))


def _is_breakpoint(path) -> bool:
    joined = "/".join(str(p) for p in path)
    return any(p.match(joined) for p in _BREAKPOINT_PATHS)


def _wire(direct_native_tool_cache: bool, cache_ttl: str = "1h") -> dict:
    messages = _transcript()
    plan = build_prompt_cache_plan(
        messages,
        copy.deepcopy(TOOLS),
        cache_ttl=cache_ttl,
        native_anthropic=True,
        static_system_prefix="STATIC PREFIX\n",
        direct_native_tool_cache=direct_native_tool_cache,
    )
    return build_anthropic_kwargs(
        model="claude-opus-4-8",
        messages=plan.messages,
        tools=plan.tools,
        max_tokens=4096,
        reasoning_config=None,
    )


@pytest.mark.parametrize("direct_native_tool_cache", [False, True])
def test_markers_only_at_breakpoint_positions(direct_native_tool_cache):
    wire = _wire(direct_native_tool_cache)
    found = list(_walk_cache_controls(wire))
    harness = [(p, v) for p, v in found if _is_breakpoint(p)]
    stray = [(p, v) for p, v in found if not _is_breakpoint(p)]

    assert harness, "a caching route must place at least one breakpoint"
    assert len(harness) <= MAX_BREAKPOINTS, harness
    for path, marker in harness:
        assert marker == {"type": "ephemeral", "ttl": "1h"}, (path, marker)

    # Only the two decoys may carry the key off-breakpoint, and byte-exact.
    stray_values = sorted(json.dumps(v, sort_keys=True) for _, v in stray)
    expected = sorted(
        json.dumps(v, sort_keys=True) for v in (_SCHEMA_DECOY, _TOOL_INPUT_DECOY)
    )
    assert stray_values == expected, stray


def test_tool_input_and_schema_decoys_reach_wire_unchanged():
    wire = _wire(direct_native_tool_cache=True)
    schema = next(t for t in wire["tools"] if t["name"] == "set_cache")["input_schema"]
    assert schema["properties"]["cache_control"] == _SCHEMA_DECOY

    tool_uses = [
        block
        for msg in wire["messages"]
        if isinstance(msg.get("content"), list)
        for block in msg["content"]
        if isinstance(block, dict) and block.get("type") == "tool_use"
        and block.get("name") == "set_cache"
    ]
    assert tool_uses and tool_uses[0]["input"] == {"cache_control": _TOOL_INPUT_DECOY}


def test_five_minute_tier_has_no_ttl_field():
    """The tier is a property of the marker, not a separate stamp: 5m = bare."""
    for path, marker in _walk_cache_controls(_wire(False, cache_ttl="5m")):
        if _is_breakpoint(path):
            assert marker == {"type": "ephemeral"}, (path, marker)
