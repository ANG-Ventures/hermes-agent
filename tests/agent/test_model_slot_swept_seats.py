"""Slot aliases P2c (t_728149cf): the seats the P3 sweep leaves holding a slot send the CURRENT id.

Proof rows from an independent sweep run on a copy of the real home (operator note on t_728149cf):
after ``scripts/model-slot-sweep.py`` these seats hold a bare slot or ``lane/slot``. Each test here
sets the view, puts the slot in the seat, and checks what the consumer sends, then flips the view
where a stale id could be pinned.

Seats already pinned on main by hermes-agent#467 (tests/agent/test_model_slot_seats.py) and not
repeated here: MoA members (test_moa_member_slot_resolves), x_search.model
(test_x_search_model_slot_resolves), the explicit aux model kwarg, delegation credentials.
"""
import json
import os
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

OPUS_ID = "claude-opus-5-5"
OPUS_NEXT = "claude-opus-5-6"
SOL_ID = "gpt-6.2-sol"
SOL_NEXT = "gpt-6.3-sol"
FABLE_ID = "claude-fable-5"
SONNET_ID = "claude-sonnet-5-5"
HAIKU_ID = "claude-haiku-5-5"
_VIEW = {"opus": OPUS_ID, "sol": SOL_ID, "fable": FABLE_ID, "sonnet": SONNET_ID, "haiku": HAIKU_ID}


def _write_view(root, slots):
    path = root / "config" / "model-slots.json"
    old = path.stat().st_mtime_ns if path.exists() else 0
    path.write_text(json.dumps({"rendered_from": "abc123", "slots": dict(slots)}), encoding="utf-8")
    st = path.stat()
    os.utime(path, ns=(st.st_atime_ns, max(st.st_mtime_ns, old) + 1_000_000_000))


@pytest.fixture
def home(tmp_path, monkeypatch):
    root = tmp_path / "vhome"
    (root / "config").mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(root))
    _write_view(root, _VIEW)
    return root


def _catalogs(monkeypatch, served):
    """Registry catalog per lane: ``served[lane]`` ids; record what each lane was asked about."""
    from hermes_cli import model_registry

    monkeypatch.setattr(model_registry, "catalog_for",
                        lambda provider, *a, **k: tuple(served.get(provider, ())))


def _turn(model, provider):
    from agent.model_slots import apply_slot_at_init

    agent = SimpleNamespace(model=model, provider=provider)
    apply_slot_at_init(agent)
    return agent.provider, agent.model


# ── kanban pins: pre-spawn check passes, the worker runs the id ────────────────

@pytest.mark.parametrize("slot,lane,ident", [("opus", "claude-alr", OPUS_ID), ("sol", "openai-codex", SOL_ID)])
def test_kanban_slot_pin_passes_prespawn_and_spawns_on_the_id(home, monkeypatch, slot, lane, ident):
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_db_dispatch import _worker_argv
    from hermes_cli.kanban_provider_health import model_override
    from hermes_cli.model_registry import invalid_pin_reason

    _catalogs(monkeypatch, {lane: (ident,)})
    kb.init_db()
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="pin", assignee="worker", model_override=slot, provider_override=lane)
        task = kb.get_task(conn, tid)
    assert (task.model_override, task.provider_override) == (slot, lane)
    # The dispatcher's pre-spawn pin check (kanban_db_dispatch invalid_pin_refused) on that row.
    model, provider = model_override(task)
    assert invalid_pin_reason(provider, model) is None
    # A lane that stopped serving the id is still caught, slot or not.
    _catalogs(monkeypatch, {lane: ("gpt-0-other",)})
    assert invalid_pin_reason(provider, model) == f"model_override_invalid: {ident} not on {lane}"
    argv = _worker_argv(task, "worker", None)
    i = len(argv) - 1 - argv[::-1].index("-m")  # the model flag, not ``python -P -m hermes_cli.main``
    assert argv[i:i + 4] == ["-m", slot, "--provider", lane]
    assert _turn(argv[i + 1], argv[i + 3]) == (lane, ident)


# ── auxiliary tasks configured by slot ─────────────────────────────────────────

@pytest.mark.parametrize("task", ["vision", "compression", "title_generation"])
@pytest.mark.parametrize("slot,ident", [("sonnet", SONNET_ID), ("haiku", HAIKU_ID)])
def test_aux_task_configured_as_slot_sends_the_id(home, task, slot, ident):
    from agent import auxiliary_client as aux

    with patch.object(aux, "_get_auxiliary_task_config", return_value={"provider": "claude-alr", "model": slot}):
        provider, model, *_ = aux._resolve_task_provider_model(task)
    assert (provider, model) == ("claude-alr", ident)


# ── fallback rungs (config chain; a cron job's chain rides the same agent path) ─

def _agent(model, provider, chain):
    from run_agent import AIAgent

    cfg = {"model": {"default": model, "provider": provider}}
    with (
        patch("hermes_cli.config.load_config", return_value=cfg),
        patch("hermes_cli.config.load_config_readonly", return_value=cfg),
        patch("model_tools.get_tool_definitions", return_value=[]),
        patch("model_tools.check_toolset_requirements", return_value={}),
        patch("agent.process_bootstrap.OpenAI"),
    ):
        agent = AIAgent(model=model, provider=provider, api_key="fake-test-token",
                        base_url="http://127.0.0.1:8317/v1", quiet_mode=True,
                        skip_context_files=True, skip_memory=True, fallback_model=chain)
    agent.client = MagicMock()
    return agent


def _activate(agent):
    client = MagicMock()
    client.base_url = "http://127.0.0.1:8318/v1"
    client.api_key = "fb-key"
    with (
        patch("agent.chat_completion_helpers._fallback_entry_unavailable_without_network", return_value=None),
        patch("agent.auxiliary_client.resolve_provider_client",
              side_effect=lambda p, model=None, **kw: (client, model)) as rpc,
        patch("hermes_cli.model_normalize.normalize_model_for_provider", side_effect=lambda m, p: m),
    ):
        assert agent._try_activate_fallback() is True
    return rpc.call_args.kwargs["model"] if rpc.call_args else agent.model


@pytest.mark.parametrize("slot,ident", [("fable", FABLE_ID), ("opus", OPUS_ID)])
def test_fallback_rung_named_by_slot_sends_the_id(home, slot, ident):
    agent = _agent("sol", "cpa", [{"provider": "claude-alr", "model": slot}])
    assert _activate(agent) == ident
    assert agent.model == ident


def test_flip_while_on_fallback_is_not_pinned_stale(home):
    """On a fallback the per-turn refresh waits (the rung keeps the id it activated with); the
    restored primary and the next activation of the rung both land on the flipped ids."""
    from agent.agent_runtime_helpers import restore_primary_runtime
    from agent.model_slots import refresh_slot_for_turn

    agent = _agent("sol", "cpa", [{"provider": "claude-alr", "model": "opus"}])
    assert agent.model == SOL_ID
    assert _activate(agent) == OPUS_ID
    _write_view(home, {**_VIEW, "sol": SOL_NEXT, "opus": OPUS_NEXT})
    assert refresh_slot_for_turn(agent) is False  # held on the fallback rung
    assert agent.model == OPUS_ID
    assert restore_primary_runtime(agent) is True
    assert agent._fallback_activated is False
    refresh_slot_for_turn(agent)
    assert agent.model == SOL_NEXT
    assert agent._build_api_kwargs([{"role": "user", "content": "hi"}])["model"] == SOL_NEXT
    # The rung resolves at each activation: after the flip it activates on the new id.
    fresh = _agent("sol", "cpa", [{"provider": "claude-alr", "model": "opus"}])
    assert fresh.model == SOL_NEXT
    assert _activate(fresh) == OPUS_NEXT


def test_cron_fallback_rung_named_by_slot_is_declared(home):
    """A cron run that rode its own ``fallback: [{opus}]`` rung ends on the id; the declared-fallback
    check must match it to the rung, not page #alerts as an undeclared switch."""
    from cron.scheduler import _fallback_was_declared

    job = {"provider": "cpa", "model": "sol", "allow_cross_provider_fallback": True,
           "fallback": [{"provider": "claude-alr", "model": "opus"}]}
    assert _fallback_was_declared(job, "claude-alr", OPUS_ID) is True
    assert _fallback_was_declared(job, "cpa", SOL_ID) is True  # ended on its own pinned slot
    assert _fallback_was_declared(job, "claude-alr", SONNET_ID) is False


# ── write-time validator: openai-codex + sol, and edits to such a cron job ─────

def test_validator_accepts_openai_codex_sol_and_cron_edits(home, monkeypatch):
    from cron import jobs as cj
    from hermes_cli.model_registry import require

    _catalogs(monkeypatch, {"openai-codex": (SOL_ID,)})
    assert require("openai-codex", "sol").ok
    job = cj.create_job(prompt="ping", schedule="in 1 hour", model="sol", provider="openai-codex")
    assert (job["model"], job["provider"]) == ("sol", "openai-codex")
    assert cj.update_job(job["id"], {"prompt": "pong"})["prompt"] == "pong"
    edited = cj.update_job(job["id"], {"model": "sol", "provider": "openai-codex"})
    assert (edited["model"], edited["provider"]) == ("sol", "openai-codex")


# ── cpa: the proxy knows ``sol`` too, the runtime still sends the registry id ──

def test_cpa_lane_sends_the_registry_id_not_the_bare_slot(home):
    assert _turn("sol", "cpa") == ("cpa", SOL_ID)
    assert _turn("cpa/sol", "cpa") == ("cpa", f"cpa/{SOL_ID}")
