"""Execute physical-call serialization and console blocks from shipped consumers."""
import ast
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent.usage_pricing import (
    USAGE_UNKNOWN_FIELDS, normalize_usage, prompt_tokens_unknown,
    format_token_count,
)

ROOT = Path(__file__).resolve().parents[2]
WIRES = [
    {"prompt_tokens": None, "completion_tokens": 50, "total_tokens": None,
     "prompt_tokens_unavailable": True, "unavailable": True},
    {"prompt_tokens": None, "completion_tokens": 50, "total_tokens": None,
     "prompt_tokens_unavailable": True, "total_tokens_unavailable": True,
     "unavailable": True,
     "prompt_tokens_details": {"cached_tokens": 100, "cache_creation_tokens": None}},
    {"prompt_tokens": None, "completion_tokens": None, "total_tokens": None,
     "unavailable": True},
    {"prompt_tokens": 150, "completion_tokens": None, "total_tokens": None,
     "output_tokens_unavailable": True},
    {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0,
     "prompt_tokens_details": {"cached_tokens": 0, "cache_creation_tokens": 0}},
]


def _execute_statements(path, anchor, namespace, count=1):
    tree = ast.parse((ROOT / path).read_text())
    matches = [n for n in ast.walk(tree) if anchor(n)]
    assert len(matches) == 1, "shipped execution seam moved"
    node = matches[0]
    parents = [n for n in ast.walk(tree)
               if isinstance(getattr(n, "body", None), list) and node in n.body]
    assert len(parents) == 1
    body = parents[0].body
    start = body.index(node)
    block = body[start:start + count]
    exec(compile(ast.Module(body=block, type_ignores=[]), str(path), "exec"), namespace)


@pytest.mark.parametrize("wire", WIRES)
@pytest.mark.parametrize("role", ["aggregator", "advisor"])
def test_moa_physical_calls_preserve_unknown_into_blackbox(wire, role):
    from agent.conversation_loop import _build_moa_pricing_calls
    from plugins.blackbox.cost import compute_turn_cost
    usage = normalize_usage(wire)
    if role == "aggregator":
        calls = _build_moa_pricing_calls([], usage, aggregator_model="claude-sonnet-4-5",
                                        aggregator_provider="anthropic", aggregator_base_url=None)
    else:
        namespace = {
            "_acct": SimpleNamespace(usage=usage, model="claude-sonnet-4-5",
                                     provider="anthropic", base_url=None),
            "_ref_pricing_calls": [], "USAGE_UNKNOWN_FIELDS": USAGE_UNKNOWN_FIELDS,
        }
        _execute_statements("agent/moa_loop.py", lambda n: isinstance(n, ast.Expr)
                            and isinstance(n.value, ast.Call)
                            and ast.unparse(n.value.func) == "_ref_pricing_calls.append", namespace)
        calls = namespace["_ref_pricing_calls"]
    for key in USAGE_UNKNOWN_FIELDS:
        assert calls[0].get(key, False) == getattr(usage, key)
    cost, status, _ = compute_turn_cost("default", "moa", None, [{"pricing_calls": calls}])
    if usage.total_tokens_unknown:
        assert cost is None
        assert status == "unknown"
    else:
        assert cost == 0
        assert status == "priced_zero"


@pytest.mark.parametrize("wire", WIRES + [
    {"prompt_tokens": 100, "completion_tokens": 50,
     "prompt_tokens_details": {"cached_tokens": 100, "cache_creation_tokens": 0}},
])
def test_console_cache_block_honors_unknown(wire):
    usage = normalize_usage(wire)
    lines = []
    namespace = {"canonical_usage": usage, "usage_dict": {"prompt_tokens": usage.prompt_tokens},
                 "prompt_tokens_unknown": prompt_tokens_unknown, "format_token_count": format_token_count,
                 "agent": SimpleNamespace(quiet_mode=False, log_prefix="", _vprint=lines.append)}
    _execute_statements("agent/conversation_loop.py", lambda n: isinstance(n, ast.Assign)
                        and any(isinstance(t, ast.Name) and t.id == "cached" for t in n.targets)
                        and ast.unparse(n.value) == "canonical_usage.cache_read_tokens", namespace, count=4)
    text = "\n".join(lines)
    if prompt_tokens_unknown(usage):
        assert "unknown" in text
        assert "% hit" not in text
        assert "0 written" not in text
    elif usage.cache_read_tokens:
        assert "100/100 tokens (100% hit, 0 written)" in text
    else:
        assert not lines


@pytest.mark.parametrize("wire", WIRES)
def test_real_moa_advisor_execution(wire, monkeypatch, tmp_path):
    from agent.moa_loop import MoAClient
    from plugins.blackbox.cost import compute_turn_cost

    (tmp_path / "config.yaml").write_text(
        "moa:\n  default_preset: default\n  presets:\n    default:\n"
        "      enabled: true\n      reference_models:\n"
        "        - provider: anthropic\n          model: claude-sonnet-4-5\n"
        "      aggregator:\n        provider: anthropic\n        model: claude-sonnet-4-5\n"
    )
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr("agent.moa_loop.call_llm", lambda **kw: SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content="advice", tool_calls=[]),
                                 finish_reason="stop")], usage=wire, model="claude-sonnet-4-5"))
    client = MoAClient("default")
    client.chat.completions.create(model="default", messages=[{"role": "user", "content": "test"}])
    calls = client.consume_reference_pricing_calls()
    assert len(calls) == 1
    cost, status, _ = compute_turn_cost("default", "moa", None, [{"pricing_calls": calls}])
    assert (cost is None) == normalize_usage(wire).total_tokens_unknown
    assert status == ("unknown" if cost is None else "priced_zero")


@pytest.mark.parametrize("wire", WIRES)
def test_verbose_log_and_thin_fallback(wire):
    from dataclasses import asdict
    from unittest.mock import Mock

    usage = normalize_usage(wire)
    log = Mock()
    ns = {"agent": SimpleNamespace(verbose_logging=True), "logging": log,
          "canonical_usage": usage, "prompt_tokens_unknown": prompt_tokens_unknown,
          "prompt_tokens": usage.prompt_tokens, "completion_tokens": usage.output_tokens,
          "total_tokens": usage.total_tokens, "output_unknown": usage.output_tokens_unknown}
    _execute_statements("agent/conversation_loop.py", lambda n: isinstance(n, ast.If)
                        and ast.unparse(n.test) == "agent.verbose_logging"
                        and "Token usage: prompt=" in ast.unparse(n), ns)
    fmt, *args = log.debug.call_args.args
    text = fmt % tuple(args)
    if prompt_tokens_unknown(usage):
        assert "prompt=unknown" in text
    if usage.total_tokens_unknown:
        assert "total=unknown" in text

    tree = ast.parse((ROOT / "gateway/slash_commands.py").read_text())
    functions = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)
                 and any(isinstance(c, ast.Assign) and any(isinstance(t, ast.Name)
                         and t.id == "lt_in" for t in c.targets) for c in n.body)]
    assert len(functions) == 1
    body = functions[0].body
    start = next(i for i, n in enumerate(body) if isinstance(n, ast.FunctionDef) and n.name == "_as_int")
    lifted = ast.FunctionDef(name="render", args=ast.arguments(posonlyargs=[], args=[],
                            kwonlyargs=[], kw_defaults=[], defaults=[]),
                            body=body[start:], decorator_list=[])
    module = ast.fix_missing_locations(ast.Module(body=[lifted], type_ignores=[]))
    ns = {"thin_snap": asdict(usage), "fallback_label": "test"}
    exec(compile(module, "thin-fallback", "exec"), ns)
    text = "\n".join(ns["render"]())
    if prompt_tokens_unknown(usage):
        assert "Tokens in: unknown" in text
    if usage.output_tokens_unknown:
        assert "Tokens out: unknown" in text
    if usage.total_tokens_unknown:
        assert "Total (billed in+out): unknown" in text


def test_verbose_log_measured_values_keep_comma_formatting():
    """MEASURED control for the sibling site: the verbose token log formatted
    measured counts with commas before the UNKNOWN routing, and must still."""
    from unittest.mock import Mock

    usage = normalize_usage({"prompt_tokens": 120_000, "completion_tokens": 8_000,
                             "total_tokens": 128_000}, api_mode="chat_completions")
    assert not prompt_tokens_unknown(usage) and not usage.total_tokens_unknown
    log = Mock()
    ns = {"agent": SimpleNamespace(verbose_logging=True), "logging": log,
          "canonical_usage": usage, "prompt_tokens_unknown": prompt_tokens_unknown,
          "prompt_tokens": usage.prompt_tokens, "completion_tokens": usage.output_tokens,
          "total_tokens": usage.total_tokens, "output_unknown": usage.output_tokens_unknown}
    _execute_statements("agent/conversation_loop.py", lambda n: isinstance(n, ast.If)
                        and ast.unparse(n.test) == "agent.verbose_logging"
                        and "Token usage: prompt=" in ast.unparse(n), ns)
    fmt, *args = log.debug.call_args.args
    text = fmt % tuple(args)

    assert "prompt=120,000" in text      # not "120k"
    assert "completion=8,000" in text
    assert "total=128,000" in text
    assert "unknown" not in text


def test_thin_fallback_measured_values_keep_comma_formatting():
    """MEASURED control: routing the unknown case through the shared rule must
    not change how a measured count renders on this card. The card's vocabulary
    is comma-grouped (``241,500``), not magnitude-abbreviated (``241.5k``)."""
    snap = {"input_tokens": 120_000, "output_tokens": 8_000, "cache_read_tokens": 110_000,
            "cache_write_tokens": 2_000, "reasoning_tokens": 1_500}

    tree = ast.parse((ROOT / "gateway/slash_commands.py").read_text())
    functions = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)
                 and any(isinstance(c, ast.Assign) and any(isinstance(t, ast.Name)
                         and t.id == "lt_in" for t in c.targets) for c in n.body)]
    assert len(functions) == 1
    body = functions[0].body
    start = next(i for i, n in enumerate(body) if isinstance(n, ast.FunctionDef) and n.name == "_as_int")
    lifted = ast.FunctionDef(name="render", args=ast.arguments(posonlyargs=[], args=[],
                            kwonlyargs=[], kw_defaults=[], defaults=[]),
                            body=body[start:], decorator_list=[])
    module = ast.fix_missing_locations(ast.Module(body=[lifted], type_ignores=[]))
    ns = {"thin_snap": snap, "fallback_label": "test"}
    exec(compile(module, "thin-fallback", "exec"), ns)
    text = "\n".join(ns["render"]())

    assert "232,000" in text                              # in billed, comma-grouped
    assert "9,500" in text                                # out billed (8,000 + 1,500 reasoning)
    assert "Total (billed in+out): 241,500" in text       # not "241.5k"
    assert "unknown" not in text                          # nothing measured reads as unknown
