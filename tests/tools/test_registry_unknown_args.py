"""dispatch() surfaces model-supplied arguments the tool schema does not declare.

A delegate_task call with `model={...}` (no such parameter) had the arg dropped by
`args.get(...)`; the children silently ran on the config default. Default behaviour: log a WARNING naming the unknown keys and the accepted
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
        out = reg.dispatch("probe", {"goal": "x", "model": {"provider": "some-provider"}})
    assert out == "ok"
    assert seen["model"] == {"provider": "some-provider"}  # handler still saw it
    assert any("['model']" in r.getMessage() and "'goal'" in r.getMessage() for r in caplog.records)


def test_strict_tool_rejects_unknown_arg_and_names_accepted_set():
    reg, seen = _reg(strict=True)
    out = reg.dispatch("probe", {"goal": "x", "model": {"provider": "some-provider"}})
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


def test_delegate_task_legacy_single_goal_shape_is_not_rejected():
    """The schema omits goal/context/role on purpose (legacy shape); strict mode must
    still accept them — only a genuinely unknown key (model=) is rejected."""
    import tools.delegate_tool  # noqa: F401
    from tools.registry import registry
    entry = registry.get_entry("delegate_task")
    props = set(entry.schema["parameters"]["properties"]) | set(entry.extra_accepted_args)
    for k in ("goal", "context", "role", "tasks", "background", "max_iterations", "output_schema", "images"):
        assert k in props, k
    assert "model" not in props

def test_delegate_task_rejects_an_imaginary_model_arg_end_to_end():
    import tools.delegate_tool  # noqa: F401
    from tools.registry import registry
    out = registry.dispatch("delegate_task", {"goal": "x", "model": {"provider": "p", "model": "m"}})
    assert "unknown argument(s) ['model']" in str(out)
