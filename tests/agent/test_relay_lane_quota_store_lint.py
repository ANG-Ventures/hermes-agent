"""Lint (t_d780729d): no new per-lane quota store for a Claude relay lane.

Every Claude relay lane reaches the same subscriptions through a relay that owns
each seat's quota state (Ace 2026-11-12 11:35, Discord 1570522498922053854). A
harness-side store of "this lane / seat is out" is the bug class: it outlives the
relay's own reset and disagrees with every sibling lane name of the same pool.

This test scans the gateway, agent, CLI, tool and plugin source for STATE that
can hold a quota / exhaustion / rate-limit verdict in a module that routes
providers: an attribute or module-level variable whose name says so, or a SQL
table that carries one. Each one found must be in ``LEDGER`` with its verdict:

* ``relay-guarded``: a routing input that consults ``agent.relay_lanes`` (or
  ``credential_pool.relay_lane_quota_is_relays``) so relay lanes never store or
  read a per-lane verdict. The test checks the module really does.
* ``relay-excluded``: written only for providers that are not relay lanes, or
  only for the LOCAL relay key (auth), or by the relay's own answer; say why.
* ``display``: instrumentation / wording only, never refuses or skips a lane.
* ``unrelated``: not provider quota at all.

A new store fails here until its author classifies it. If it can hold a quota
verdict for a relay lane, guard it with ``agent.relay_lanes.is_relay_backed``
and mark it ``relay-guarded``; a lane's quota comes from the relay's answer at
decision time (``/eligibility`` or its typed refusal), not from a stored row.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCAN = ("agent", "gateway", "hermes_cli", "tools", "plugins", "providers")
NAME_RE = re.compile(r"(?i)(exhaust|quota|rate_?limit|bench|cooldown)")
SQL_RE = re.compile(r"(?is)\bcreate\s+table\b.*?\b(\w*(until|exhaust|quota|cooldown|bench)\w*)\b")
# A module "routes providers" when it talks about them; others cannot hold a lane verdict.
ROUTES_RE = re.compile(r"\bprovider\b")
RELAY_GUARD_RE = re.compile(r"\b(relay_lanes|relay_lane_quota_is_relays|is_relay_backed|"
                            r"is_relay_pooled|skips_legacy_arm|_relay_pooled)\b")

# (module, name) -> (verdict, who writes / reads / clears it). Keep one line each.
LEDGER: dict[tuple[str, str], tuple[str, str]] = {
    # ── the per-lane quota stores this card fixed ──
    ("agent/credential_pool.py", "~auth.json credential_pool.last_status"): (
        "relay-guarded", "row status; not an AST store, listed so its module guard is checked"),
    ("agent/fallback_policy.py", "~arm_sticky quota until"): (
        "relay-guarded", "sticky quota cache lifetime: relay reset or RELAY_QUOTA_CACHE_S"),
    ("agent/fallback_sticky_store.py", "SQL:fallback_sticky"): (
        "relay-guarded", "turns.db episode; quota until on relay lanes = relay reset or 5 min (fallback_policy.arm_sticky)"),
    ("agent/chat_completion_helpers.py", "_rate_limited_until"): (
        "relay-guarded", "legacy per-agent bench; skipped for relay quota classes (fallback_policy.skips_legacy_arm)"),
    ("agent/fallback_cooldown.py", "_rate_limited_until"): (
        "relay-excluded", "_arm_rate_limit_cooldown has no caller on main; dead"),
    ("hermes_cli/cli_model_switch_mixin.py", "_rate_limited_until"): ("relay-excluded", "/model clears it to 0 only"),
    ("agent/quota_registry_gate.py", "_quota_gate_applied"): (
        "relay-excluded", "usage.json snapshot (relay-published), skipped for relay quota_seat; per-turn flag"),
    ("agent/quota_registry_gate.py", "_quota_gate_skipped_providers"): (
        "relay-excluded", "per-turn chain-walk skip list from the published usage snapshot, rebuilt every failover"),
    ("agent/quota_registry_gate.py", "_quota_gate_soonest_reset_text"): ("display", "terminal-failure wording"),
    ("agent/quota_registry_gate.py", "_quota_gate_status_emitted"): ("display", "one status line per turn"),
    ("agent/chat_completion_helpers.py", "_pending_quota_window"): ("display", "failover notice window text"),
    ("agent/turn_api_error.py", "_pending_quota_window"): ("display", "failover notice window text"),
    ("hermes_cli/cli_agent_setup_mixin.py", "_credentials_rate_limited"): (
        "relay-excluded", "startup AuthError flag for this process only; exit code"),
    ("hermes_cli/cli_agent_setup_mixin.py", "_kanban_pin_rate_limited"): (
        "relay-excluded", "worker exit flag; kanban cooling_providers reads auth-stage refusals only"),
    ("hermes_cli/cli_init_mixin.py", "_kanban_pin_rate_limited"): ("relay-excluded", "same flag, init"),
    ("hermes_cli/kanban_db_dispatch.py", "_last_rate_limited"): ("display", "dispatcher log de-dup"),
    ("hermes_cli/auth_codex.py", "_codex_quota_probe_cache"): ("relay-excluded", "openai-codex only"),
    ("hermes_cli/auth_codex.py", "_codex_quota_probe_lock"): ("relay-excluded", "openai-codex only"),
    ("agent/agent_runtime_helpers.py", "_rate_limit_backoff_count"): (
        "relay-guarded", "escalation counter of _rate_limited_until; reset on restore"),
    ("agent/chat_completion_helpers.py", "_pool_exhausted_models"): (
        "relay-guarded", "per-turn model set from the relay's own quota_model answer this turn; skips "
        "relay faces (is_relay_backed) for that model only; reset at turn start (t_5188f4ed)"),
    ("agent/chat_completion_helpers.py", "_rate_limit_backoff_count"): (
        "relay-guarded", "escalation counter of _rate_limited_until; relay quota classes skip the arm"),
    ("agent/fallback_cooldown.py", "_rate_limit_backoff_count"): ("relay-excluded", "dead helper, no caller"),
    ("agent/rate_limit_credits.py", "_rate_limit_state"): (
        "display", "last response's rate-limit headers for /usage and notices; never refuses a lane"),
    ("plugins/blackbox/store.py", "SQL:turns"): (
        "display", "per-turn instrumentation (cooldown_s column = what the failover row recorded)"),
    ("hermes_cli/kanban_db.py", "SQL:tasks"): ("unrelated", "card table (until = defer time)"),
    # ── not provider quota ──
    ("plugins/context_engine/lcm/db_bootstrap.py", "SQL:lcm_assertion_sources"): ("unrelated", "'quotation' column"),
    ("plugins/platforms/discord/adapter.py", "max_ratelimit_timeout"): ("unrelated", "discord.py client knob"),
    ("agent/context_compressor.py", "_cooldown_persist_failed"): ("unrelated", "compression summary retry"),
    ("agent/context_compressor.py", "_last_cooldown_refresh_was_authoritative"): ("unrelated", "compression"),
    ("agent/context_compressor.py", "_summary_failure_cooldown_until"): ("unrelated", "compression"),
    ("agent/turn_preflight.py", "_compression_timeout_exhausted"): ("unrelated", "compression"),
    ("gateway/run.py", "_scale_to_zero_cooldown_until"): ("unrelated", "gateway idle scale"),
    ("gateway/run_shutdown.py", "_scale_to_zero_cooldown_until"): ("unrelated", "gateway idle scale"),
    ("gateway/run_turn.py", "failure_cooldown_seconds"): ("unrelated", "session hygiene"),
    ("hermes_cli/kanban_db.py", "budget_exhausted"): ("unrelated", "card budget"),
    ("hermes_cli/kanban_pr_gate.py", "budget_exhausted"): ("unrelated", "PR budget"),
    ("plugins/memory/mem0/capture_router.py", "_primary_cooldown_until"): ("unrelated", "mem0 endpoint"),
    ("plugins/memory/mem0/capture_router.py", "_primary_cooldown_lock"): ("unrelated", "mem0 endpoint"),
    ("plugins/context_engine/lcm/adaptive_retrieval.py", "budget_exhausted"): ("unrelated", "lcm"),
    ("plugins/context_engine/lcm/compaction.py", "_preflight_cleanup_only_due_to_boundary_cooldown"): ("unrelated", "lcm"),
    ("plugins/context_engine/lcm/engine.py", "_preflight_cleanup_only_due_to_boundary_cooldown"): ("unrelated", "lcm"),
    ("plugins/platforms/telegram/adapter.py", "_telegram_typing_cooldown_until"): ("unrelated", "typing"),
    ("plugins/platforms/telegram/adapter.py", "_telegram_typing_cooldown_seconds"): ("unrelated", "typing"),
    ("tools/skills_hub_github.py", "_rate_limited"): ("unrelated", "GitHub API"),
}
VERDICTS = {"relay-guarded", "relay-excluded", "display", "unrelated"}


def _is_constant(name: str) -> bool:
    return name.lstrip("_").isupper() or name.lstrip("_")[:1].isupper()


def _module_scope(tree: ast.Module) -> set:
    """ids of every statement that runs at module scope, including under a
    module-level ``if`` / ``try`` / ``with`` / ``for`` (not inside a def/class)."""
    out: set = set()
    stack = list(tree.body)
    while stack:
        node = stack.pop()
        out.add(id(node))
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.stmt):
                stack.append(child)
            elif isinstance(child, ast.ExceptHandler) or (
                    hasattr(ast, "match_case") and isinstance(child, ast.match_case)):
                stack.extend(child.body)
    return out


def _stores(tree: ast.Module):
    top = _module_scope(tree)
    for node in ast.walk(tree):
        targets = []
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, (ast.AugAssign, ast.AnnAssign)):
            targets = [node.target]
        for t in targets:
            while isinstance(t, ast.Subscript):
                t = t.value
            if isinstance(t, ast.Attribute):
                name = t.attr
            elif isinstance(t, ast.Name) and id(node) in top:
                name = t.id  # module-level state only
            else:
                continue
            if NAME_RE.search(name) and not _is_constant(name):
                yield name
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            m = SQL_RE.search(node.value)
            if m:
                table = re.search(r"(?is)create\s+table\s+(?:if\s+not\s+exists\s+)?(\w+)", node.value)
                if table:
                    yield f"SQL:{table.group(1)}"


def _scan():
    found: dict[tuple[str, str], str] = {}
    for base in SCAN:
        for f in sorted((ROOT / base).rglob("*.py")):
            rel = f.relative_to(ROOT).as_posix()
            if "/tests/" in rel or f.name.startswith(("test_", "conftest")):
                continue
            text = f.read_text(encoding="utf-8", errors="replace")
            if not ROUTES_RE.search(text):
                continue
            try:
                tree = ast.parse(text)
            except SyntaxError:
                continue
            for name in _stores(tree):
                found[(rel, name)] = text
    return found


@pytest.fixture(scope="module")
def found():
    return _scan()


def test_every_quota_shaped_store_is_classified(found):
    unknown = sorted(k for k in found if k not in LEDGER)
    assert not unknown, (
        "new quota/exhaustion/cooldown state in a provider-routing module; classify it in "
        "LEDGER (tests/agent/test_relay_lane_quota_store_lint.py). If it can hold a quota "
        "verdict for a Claude relay lane, guard it with agent.relay_lanes.is_relay_backed: "
        "the relay's answer decides, not a stored per-lane row (t_d780729d):\n  "
        + "\n  ".join(f"{p}: {n}" for p, n in unknown))


def test_ledger_verdicts_are_known():
    bad = {k: v for k, v in LEDGER.items() if v[0] not in VERDICTS or not v[1].strip()}
    assert not bad


def test_ledger_has_no_stale_rows(found):
    """A removed store leaves no row behind (the ledger is the inventory)."""
    stale = sorted(k for k in LEDGER if not k[1].startswith("~") and k not in found)
    assert not stale, stale


@pytest.mark.parametrize("module", sorted({p for (p, _n), (v, _w) in LEDGER.items()
                                           if v == "relay-guarded"}))
def test_relay_guarded_modules_consult_the_relay_predicate(module):
    text = (ROOT / module).read_text(encoding="utf-8")
    if module == "agent/fallback_sticky_store.py":
        # the store is guarded at its only quota writer
        text = (ROOT / "agent/fallback_policy.py").read_text(encoding="utf-8")
    elif module == "agent/chat_completion_helpers.py":
        text = (ROOT / "agent/fallback_wiring.py").read_text(encoding="utf-8")
    assert RELAY_GUARD_RE.search(text), f"{module} is marked relay-guarded but never consults agent.relay_lanes"


def _func(path: str, name: str) -> ast.FunctionDef:
    tree = ast.parse((ROOT / path).read_text(encoding="utf-8"))
    return next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == name)


@pytest.mark.parametrize("path,func", [
    ("agent/credential_pool.py", "_mark_exhausted"),        # the one writer of last_status=exhausted
    ("agent/credential_pool.py", "_exhausted_until"),       # the one reader every bench goes through
    ("agent/credential_pool.py", "mark_exhausted_and_rotate"),
    ("agent/fallback_policy.py", "arm_sticky"),             # the one quota writer of fallback_sticky
    ("agent/agent_runtime_helpers.py", "_recover_rate_limit"),
])
def test_choke_points_carry_the_relay_guard(path, func):
    src = ast.unparse(_func(path, func))
    assert re.search(r"relay_lane_quota_is_relays|_relay_pooled|is_relay_backed", src), (
        f"{path}:{func} lost its relay-lane guard (t_d780729d)")


def test_lint_catches_a_new_store(tmp_path, monkeypatch):
    """Mutation canary: a planted per-lane store in a provider module is reported."""
    mod = tmp_path / "agent"
    mod.mkdir()
    (mod / "lane_quota.py").write_text(
        "provider = 'claude-alr'\n"
        "class X:\n"
        "    def f(self):\n"
        "        self._lane_exhausted_until = {provider: 1}\n"
        "        _local_quota = 1\n"
        "if provider:\n"
        "    _if_lane_quota = {}\n"
        "try:\n"
        "    _try_lane_exhausted = {}\n"
        "except Exception:\n"
        "    _except_lane_cooldown = {}\n"
        "with open(__file__):\n"
        "    _with_lane_bench = {}\n")
    monkeypatch.setattr(__import__(__name__, fromlist=["x"]), "ROOT", tmp_path)
    found = {n for (p, n) in _scan() if p == "agent/lane_quota.py"}
    assert {"_lane_exhausted_until", "_if_lane_quota", "_try_lane_exhausted",
            "_except_lane_cooldown", "_with_lane_bench"} <= found, found
    assert "_local_quota" not in found  # a function local is not module state
