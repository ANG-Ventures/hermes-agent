"""dispatch() surfaces model-supplied arguments the tool schema does not declare.

2026-09-09: delegate_task was called with `model={...}` (no such parameter); the arg was
dropped by `args.get(...)`, the children ran on the capped config default and 429'd, and
nothing said why. Default behaviour: log a WARNING naming the unknown keys and the accepted
set (handlers like execute_code legitimately inspect stray keys to give a better error, and
hook tests inject probe keys, so a blanket reject breaks them). Tools that opt in with
`strict_args=True` (delegate_task) fail the call loudly instead.
"""
import logging

from tools.registry import ToolRegistry


def _reg(strict=False, schema_extra=None):
    reg = ToolRegistry()
    schema = {"name": "probe", "parameters": {"type": "object", "properties": {"goal": {"type": "string"}}}}
    if schema_extra is not None:
        schema["parameters"]["additionalProperties"] = schema_extra
    seen = {}
    reg.register(name="probe", toolset="t", schema=schema, strict_args=strict,
                 handler=lambda args, **kw: seen.update(args) or "ok")
    return reg, seen


def test_unknown_arg_is_logged_and_still_dispatched_by_default(caplog):
    reg, seen = _reg()
    with caplog.at_level(logging.WARNING, logger="tools.registry"):
        out = reg.dispatch("probe", {"goal": "x", "model": {"provider": "claude-apr"}})
    assert out == "ok"
    assert seen["model"] == {"provider": "claude-apr"}  # handler still saw it
    assert any("['model']" in r.getMessage() and "'goal'" in r.getMessage() for r in caplog.records)


def test_strict_tool_rejects_unknown_arg_and_names_accepted_set():
    reg, seen = _reg(strict=True)
    out = reg.dispatch("probe", {"goal": "x", "model": {"provider": "claude-apr"}})
    assert "unknown argument(s) ['model']" in str(out)
    assert "'goal'" in str(out)
    assert seen == {}  # handler never ran


def test_known_args_dispatch_normally_even_when_strict():
    reg, seen = _reg(strict=True)
    assert reg.dispatch("probe", {"goal": "x"}) == "ok"
    assert seen == {"goal": "x"}


def test_additional_properties_true_opts_out_of_both():
    reg, seen = _reg(strict=True, schema_extra=True)
    assert reg.dispatch("probe", {"goal": "x", "anything": 1}) == "ok"
    assert seen["anything"] == 1


def test_delegate_task_is_registered_strict():
    import tools.delegate_tool  # noqa: F401 — registers the tool
    from tools.registry import registry
    assert registry.get_entry("delegate_task").strict_args is True
