"""Every ``kanban.*`` key the dispatcher reads must be in DEFAULT_CONFIG.

``hermes config set kanban.max_spawn 128`` printed "not a recognized config
key" (deploy readback 2026-09-25) even though the gateway dispatcher reads
``kanban_cfg.get("max_spawn")``: ``_validate_config_key`` walks DEFAULT_CONFIG
and the key was never declared there. The same was true of
``review_assignee``, ``review_stale_minutes`` and ``home_guard``.

A warning that is wrong by construction trains operators to ignore the one
signal that catches a genuinely inert knob. This test derives the key set from
the source (AST scan of ``.get("<key>")`` on any value taken from
``.get("kanban")``), so a new knob fails here until it is declared.
"""

import ast
from pathlib import Path

import pytest

import hermes_cli.config as config_module
from hermes_cli.config import DEFAULT_CONFIG, _validate_config_key

PKG_DIR = Path(config_module.__file__).resolve().parent
REPO = PKG_DIR.parent

READER_FILES = (
    REPO / "gateway" / "kanban_watchers.py",
    PKG_DIR / "kanban.py",
    PKG_DIR / "kanban_db.py",
)
LOAD_GATE = PKG_DIR / "kanban_load_gate.py"


def _is_get(node, key=None):
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "get"
        and bool(node.args)
        and isinstance(node.args[0], ast.Constant)
        and isinstance(node.args[0].value, str)
        and (key is None or node.args[0].value == key)
    )


def kanban_keys_read(source: str) -> set[str]:
    """Keys read via ``<x>.get("k")`` where ``<x>`` came from ``.get("kanban")``.

    Covers both a bound name (``kanban_cfg = cfg.get("kanban", {})`` then
    ``kanban_cfg.get("k")``) and the chained form
    (``load_config().get("kanban", {}).get("k")``), with or without an
    ``(... or {})`` wrapper around the receiver.
    """
    tree = ast.parse(source)
    names = {
        target.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        and any(_is_get(n, "kanban") for n in ast.walk(node.value))
        for target in node.targets
        if isinstance(target, ast.Name)
    }
    keys = set()
    for node in ast.walk(tree):
        if not _is_get(node):
            continue
        recv = node.func.value
        if isinstance(recv, ast.BoolOp):
            recv = recv.values[0]
        bound = isinstance(recv, ast.Name) and recv.id in names
        if (bound or _is_get(recv, "kanban")) and node.args[0].value != "kanban":
            keys.add(node.args[0].value)
    # #1075 routes the board-scoped review knobs through
    # ``_kanban_review_setting("<key>", default)`` (board home -> profile
    # config) instead of a direct ``.get`` -- the key is still a kanban.* key.
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "_kanban_review_setting"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            keys.add(node.args[0].value)
    return keys


def _all_keys_read() -> set[str]:
    keys = set()
    for path in READER_FILES:
        keys |= kanban_keys_read(path.read_text())
    return keys


def test_scanner_finds_the_dispatcher_keys():
    """Positive control: a scanner matching nothing would make this vacuous."""
    keys = _all_keys_read()
    assert {"max_spawn", "dispatch_in_gateway", "dispatch_load_gate",
            "review_assignee"} <= keys, keys
    assert len(keys) >= 12, keys


def test_scanner_flags_an_undeclared_key():
    """Negative control: an unknown key in source is collected and would fail."""
    src = (
        'kanban_cfg = cfg.get("kanban", {})\n'
        'kanban_cfg.get("no_such_knob_xyz")\n'
        'load_config().get("kanban", {}).get("other_knob_xyz")\n'
    )
    keys = kanban_keys_read(src)
    assert keys == {"no_such_knob_xyz", "other_knob_xyz"}
    assert "no_such_knob_xyz" not in DEFAULT_CONFIG["kanban"]


@pytest.mark.parametrize("knob", sorted(_all_keys_read()))
def test_every_kanban_key_read_is_declared(knob):
    key = f"kanban.{knob}"
    assert knob in DEFAULT_CONFIG["kanban"], (
        f"{key} is read by the kanban dispatcher but is not declared in "
        "config_defaults.py's kanban block, so `hermes config set` warns it "
        "is unrecognized. Declare it there with the same default the code uses."
    )
    assert _validate_config_key(key) == (True, None)


def _load_gate_keys() -> set[str]:
    tree = ast.parse(LOAD_GATE.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "LoadGate":
            return {
                n.args[0].value
                for n in ast.walk(node)
                if _is_get(n)
                and isinstance(n.func.value, ast.Name)
                and n.func.value.id == "cfg"
            }
    raise AssertionError("LoadGate class not found")


def test_dispatch_load_gate_subkeys_are_declared():
    keys = _load_gate_keys()
    assert "max_spawn_per_tick" in keys, keys  # positive control
    declared = DEFAULT_CONFIG["kanban"]["dispatch_load_gate"]
    missing = sorted(keys - set(declared))
    assert not missing, f"kanban.dispatch_load_gate keys read but undeclared: {missing}"
    for knob in keys:
        assert _validate_config_key(f"kanban.dispatch_load_gate.{knob}") == (True, None)


def test_per_profile_cap_accepts_profile_names():
    """``max_in_progress_per_profile`` may be a {profile: cap} map."""
    assert _validate_config_key("kanban.max_in_progress_per_profile.daedalus") == (True, None)
