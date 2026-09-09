"""dispatch() rejects model-supplied arguments the tool schema does not declare.

2026-09-09: delegate_task was called with `model={...}` (no such parameter); the arg was
dropped by `args.get(...)`, the children ran on the capped config default and 429'd, and
nothing said why. An unknown key must fail the call loudly and name what IS accepted.
"""
from tools.registry import ToolRegistry


def _reg(schema_extra=None):
    reg = ToolRegistry()
    schema = {"name": "probe", "parameters": {"type": "object", "properties": {"goal": {"type": "string"}}}}
    if schema_extra is not None:
        schema["parameters"]["additionalProperties"] = schema_extra
    seen = {}
    reg.register(name="probe", toolset="t", schema=schema,
                 handler=lambda args, **kw: seen.update(args) or "ok")
    return reg, seen


def test_unknown_arg_is_rejected_and_named():
    reg, seen = _reg()
    out = reg.dispatch("probe", {"goal": "x", "model": {"provider": "claude-apr"}})
    assert "unknown argument(s) ['model']" in str(out)
    assert "'goal'" in str(out)  # the accepted set is listed
    assert seen == {}  # handler never ran


def test_known_args_dispatch_normally():
    reg, seen = _reg()
    assert reg.dispatch("probe", {"goal": "x"}) == "ok"
    assert seen == {"goal": "x"}


def test_additional_properties_true_opts_out():
    reg, seen = _reg(schema_extra=True)
    assert reg.dispatch("probe", {"goal": "x", "anything": 1}) == "ok"
    assert seen["anything"] == 1
