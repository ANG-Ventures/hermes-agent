"""Read-only toolsets (fork-custom): ``file_read`` and ``skills_read``.

Reviewer profiles (Momus) must be unable to write by construction. Toolsets
are atomic, so these read-only subsets are the only way to keep read_file /
skill_view while dropping write_file / patch / skill_manage.
"""

import pytest

from toolsets import TOOLSETS, resolve_toolset
from hermes_cli.tools_config import (
    CONFIGURABLE_TOOLSETS,
    _DEFAULT_OFF_TOOLSETS,
    _get_platform_tools,
)

WRITE_TOOLS = {"write_file", "patch", "skill_manage", "terminal", "execute_code"}


@pytest.mark.parametrize(
    "name, expected",
    [
        ("file_read", {"read_file", "search_files"}),
        ("skills_read", {"skills_list", "skill_view"}),
    ],
)
def test_readonly_toolset_resolves_exactly(name, expected):
    assert name in TOOLSETS
    assert set(resolve_toolset(name)) == expected
    assert set(resolve_toolset(name, include_registry=False)) == expected


def test_readonly_toolsets_are_configurable_and_opt_in():
    keys = {k for k, _, _ in CONFIGURABLE_TOOLSETS}
    assert {"file_read", "skills_read"} <= keys
    assert {"file_read", "skills_read"} <= _DEFAULT_OFF_TOOLSETS


def test_explicit_readonly_platform_config_exposes_no_write_tool():
    from model_tools import get_tool_definitions

    cfg = {"platform_toolsets": {"cli": ["file_read", "skills_read", "todo"]}}
    enabled = _get_platform_tools(cfg, "cli", include_default_mcp_servers=False)
    assert {"file_read", "skills_read"} <= set(enabled)
    assert "file" not in enabled and "skills" not in enabled

    defs = get_tool_definitions(
        enabled_toolsets=sorted(enabled),
        quiet_mode=True,
        skip_tool_search_assembly=True,
    )
    names = {d["function"]["name"] for d in defs if "function" in d}
    assert {"read_file", "search_files", "skill_view"} <= names
    assert not (names & WRITE_TOOLS), sorted(names & WRITE_TOOLS)


def test_default_composite_does_not_auto_enable_readonly_subsets():
    # Implicit (composite) resolution must not add the read-only subsets,
    # otherwise unchecking ``file`` would leave read_file enabled.
    enabled = _get_platform_tools({}, "cli", include_default_mcp_servers=False)
    assert "file" in enabled
    assert "file_read" not in enabled and "skills_read" not in enabled
