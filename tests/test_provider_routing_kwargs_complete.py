"""Every AIAgent construction passes all six provider_routing filter kwargs or none (t_145e2be9).

A path that passes some of them drops the rest silently: before this card cron agents carried
only/ignore/order/sort and so ran with no ``require_parameters`` and no ``data_collection`` deny.
This scans the source for any ``AIAgent(...)`` call or kwargs dict literal that names some but
not all six, so a fourth path cannot drift. A site that wants the config block's filters spreads
``gateway.run_config_loaders.provider_routing_agent_kwargs(pr)``, which names none of them.
"""

from __future__ import annotations

import ast
import warnings
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
ROUTING_KWARGS = frozenset({
    "providers_allowed", "providers_ignored", "providers_order", "provider_sort",
    "provider_require_parameters", "provider_data_collection",
})
# evals/: one-off live probes that pin a provider on purpose, not config-driven agent paths.
SKIP_DIRS = {"tests", "evals", ".git", ".worktrees", "node_modules", ".venv", "venv", "build", "dist"}


def _source_files():
    for path in REPO.rglob("*.py"):
        rel = path.relative_to(REPO)
        if SKIP_DIRS.intersection(rel.parts[:-1]):
            continue
        yield path, rel


def _partial(names: set[str]) -> set[str] | None:
    """The missing routing kwargs when ``names`` carries some but not all six, else None."""
    present = names & ROUTING_KWARGS
    if present and present != ROUTING_KWARGS:
        return set(ROUTING_KWARGS - present)
    return None


def _is_agent_call(node: ast.AST) -> bool:
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    return (func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)) == "AIAgent"


def _routing_dict_key(node: ast.AST) -> bool:
    return isinstance(node, ast.Dict) and any(
        isinstance(k, ast.Constant) and k.value in ROUTING_KWARGS for k in node.keys)


def partial_sites(tree: ast.AST) -> list[tuple[int, str, set[str]]]:
    """Construction sites naming some but not all six routing kwargs.

    The unit is the function: one that calls ``AIAgent(...)`` or builds a dict keyed by a routing
    kwarg is a construction site, and every routing name it spells (keyword, dict key, or a string
    in a key tuple it iterates) counts, since kwargs are often assembled across several literals."""
    hits = []
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        nodes = list(ast.walk(fn))
        if not any(_is_agent_call(n) or _routing_dict_key(n) for n in nodes):
            continue
        names = {kw.arg for n in nodes if isinstance(n, ast.Call) for kw in n.keywords if kw.arg}
        names |= {n.value for n in nodes if isinstance(n, ast.Constant) and isinstance(n.value, str)}
        missing = _partial(names)
        if missing:
            hits.append((fn.lineno, f"{fn.name}()", missing))
    return hits


def test_no_agent_construction_passes_a_partial_routing_filter_set():
    offenders = []
    for path, rel in _source_files():
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", SyntaxWarning)
                tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(rel))
        except (SyntaxError, UnicodeDecodeError):
            continue
        for lineno, kind, missing in partial_sites(tree):
            offenders.append(f"{rel}:{lineno} {kind} missing {sorted(missing)}")
    assert not offenders, (
        "AIAgent routing filters passed partially; spread "
        "gateway.run_config_loaders.provider_routing_agent_kwargs(pr) instead:\n" + "\n".join(offenders))


def test_detector_flags_a_partial_call_and_passes_full_or_spread():
    def fn(body: str) -> ast.AST:
        return ast.parse(f"def build():\n    {body}\n")

    partial = fn("return AIAgent(model='m', providers_allowed=a, providers_ignored=b)")
    full = fn("return AIAgent(" + ", ".join(f"{k}=None" for k in sorted(ROUTING_KWARGS)) + ")")
    spread = fn("return AIAgent(model='m', **provider_routing_agent_kwargs(pr))")
    partial_dict = fn("return {'providers_allowed': a, 'provider_sort': s}")
    assert partial_sites(partial) and partial_sites(partial_dict)
    assert not partial_sites(full) and not partial_sites(spread)
