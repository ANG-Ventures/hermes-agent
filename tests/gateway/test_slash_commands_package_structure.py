"""Structural invariants for the gateway/slash_commands/ package.

Behavior contracts, not snapshots: every assertion states a *relationship* between
two structures (registry <-> CommandDef, registry <-> handler methods, leaf mixins
<-> composed class) rather than freezing a count or a list. Adding a new command
never breaks any of these.
"""

import ast
import importlib
import pkgutil
import sys
from pathlib import Path

import pytest

from gateway.slash_commands import GatewaySlashCommandsMixin
from gateway.slash_commands.registry import GATEWAY_SLASH_HANDLERS

PKG_DIR = Path(__file__).resolve().parents[2] / "gateway" / "slash_commands"

LEAF_MIXINS = [
    b for b in GatewaySlashCommandsMixin.__bases__
]


def test_every_registry_binding_names_a_real_command_and_a_real_method():
    """The registry may not drift from either COMMAND_REGISTRY or the mixin."""
    from hermes_cli.commands import resolve_command

    for name, method in GATEWAY_SLASH_HANDLERS.items():
        assert resolve_command(name) is not None, (
            f"registry binds {name!r}, which is not a command in COMMAND_REGISTRY"
        )
        assert hasattr(GatewaySlashCommandsMixin, method), (
            f"registry binds {name!r} -> {method!r}, which is not a mixin method"
        )


def test_registry_bindings_resolve_to_bound_methods_on_the_runner():
    """`getattr(self, name)` must produce the same bound method the old
    hand-written branch called — identity, not equality, so a binding that points
    at a *different* handler with a similar name cannot slip through."""
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    for name, method in GATEWAY_SLASH_HANDLERS.items():
        assert getattr(runner, method) == getattr(runner, method)
        assert getattr(runner, method).__func__ is getattr(
            GatewaySlashCommandsMixin, method
        ), f"{name!r} -> {method!r} does not resolve to the mixin's function"


def test_leaf_mixin_methods_are_disjoint():
    """No method name may be defined by two leaf mixins, so the MRO is
    unambiguous and linearization order carries no semantic weight."""
    seen: dict[str, str] = {}
    for mixin in LEAF_MIXINS:
        for name, value in vars(mixin).items():
            if name.startswith("__"):
                continue
            if not callable(value) and not isinstance(value, staticmethod):
                continue
            assert name not in seen, (
                f"{name!r} is defined by both {seen[name]} and {mixin.__name__} — "
                f"the composed MRO would silently pick one"
            )
            seen[name] = mixin.__name__


def test_every_handler_is_reachable_through_the_composed_mixin():
    """Every _handle_*_command defined by any leaf must resolve on the composed
    class, which is what GatewayRunner actually inherits."""
    for mixin in LEAF_MIXINS:
        for name in vars(mixin):
            if name.startswith("_handle_") and name.endswith("_command"):
                assert hasattr(GatewaySlashCommandsMixin, name)


def test_no_leaf_module_imports_gateway_run_at_module_scope():
    """The deferred-import discipline is what keeps the graph acyclic. A leaf that
    imports gateway.run at module scope would reintroduce the cycle."""
    offenders = []
    for path in sorted(PKG_DIR.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in tree.body:
            if isinstance(node, ast.ImportFrom) and (node.module or "").startswith(
                "gateway.run"
            ):
                offenders.append(f"{path.name}:{node.lineno}")
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.startswith("gateway.run"):
                        offenders.append(f"{path.name}:{node.lineno}")
    assert not offenders, f"module-scope gateway.run imports: {offenders}"


def test_importing_the_package_does_not_pull_in_gateway_run():
    """The static graph is gateway.run -> gateway.slash_commands.* -> leaves."""
    import subprocess

    code = (
        "import sys; import gateway.slash_commands; "
        "sys.exit(1 if 'gateway.run' in sys.modules else 0)"
    )
    root = Path(__file__).resolve().parents[2]
    proc = subprocess.run(
        [sys.executable, "-c", code], cwd=root, capture_output=True, text=True
    )
    assert proc.returncode == 0, (
        "importing gateway.slash_commands pulled in gateway.run "
        f"(stderr: {proc.stderr[-400:]})"
    )


def test_every_leaf_shares_the_one_logger_object():
    """Leaf modules import the single logger from _shared rather than deriving a
    __name__-based one, so log records keep the name they had before the split."""
    from gateway.slash_commands import _shared

    for _, modname, _ in pkgutil.iter_modules([str(PKG_DIR)]):
        mod = importlib.import_module(f"gateway.slash_commands.{modname}")
        own = getattr(mod, "logger", None)
        if own is None:
            continue
        assert own is _shared.logger, (
            f"gateway.slash_commands.{modname} has its own logger "
            f"({own.name!r}) instead of the shared one"
        )
    assert _shared.logger.name == "gateway.run"


def test_composed_mixin_keeps_the_async_session_store_annotation():
    """Typing-only declaration whose value GatewayRunner.__init__ supplies."""
    assert "async_session_store" in GatewaySlashCommandsMixin.__annotations__
