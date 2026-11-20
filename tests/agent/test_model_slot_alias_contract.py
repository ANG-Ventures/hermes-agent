"""Slot aliases P2c design cut (t_728149cf): a config alias points at ``<lane>/<slot>``.

The P3 sweep (hermes-home ``scripts/model-slot-sweep.py``) rewrites ``model.aliases`` values from
literal ids (``sol: cpa/gpt-6.2-sol``) to the slot on its lane (``sol: cpa/sol``). With that, the
alias names the lane and the slot names the model; the per-turn slot resolution that is already
merged (#466/#467) does the rest. No "slot wins over alias" precedence and no vendor-lane refusal.

This file pins the contract the sweep relies on, at every writer and seat:
(a) ``/model sol`` stores the slot on the alias lane and the next turn sends the current id;
(b) ``hermes -m sol`` at startup runs the current id on the alias lane, and follows a flip;
(c) a kanban pin / cron job ``sol`` is stored as the slot on the alias lane, dispatched as the id;
(d) ``delegate_task(model='sol')`` runs the id on the alias lane;
(e) write-time catalog validation sees the resolved id, never the string ``sol``;
(f) literal aliases (``sol: cpa/gpt-6.2-sol``) behave as on main, except that a writer stores the
    slot's current id as the slot (P6, t_efde9d1a).
Plus the seams carried from hermes-agent#469: the two aux seams that read config model.default
directly, and ``slot_view`` keeping the last good parse on a torn file.
"""
import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
import yaml

SOL_ID = "gpt-6.2-sol"
SOL_NEXT = "gpt-6.3-sol"
OPUS_ID = "claude-opus-5-5"
_LANES = {
    "cpa": {"base_url": "http://127.0.0.1:8317/v1", "api_key": "fake-test-token"},
    "claude-alr": {"base_url": "http://127.0.0.1:8318/v1", "api_key": "fake-test-token"},
}
# The two spellings the sweep writes: the string form and the dict form.
_ALIAS_FORMS = {
    "string": "cpa/sol",
    "dict": {"provider": "cpa", "model": "sol"},
}


def _write_view(root, slots):
    path = root / "config" / "model-slots.json"
    old = path.stat().st_mtime_ns if path.exists() else 0
    path.write_text(json.dumps({"rendered_from": "abc123", "slots": dict(slots)}), encoding="utf-8")
    st = path.stat()
    bump = max(st.st_mtime_ns, old) + 1_000_000_000
    os.utime(path, ns=(st.st_atime_ns, bump))


def _reset_aliases():
    from hermes_cli import model_switch as ms

    ms.DIRECT_ALIASES.clear()
    ms._DIRECT_ALIAS_LOADED = None
    ms._DIRECT_ALIASES_DEGRADED = False


@pytest.fixture
def vhome(tmp_path, monkeypatch):
    root = tmp_path / ".hermes"
    (root / "config").mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(root))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    _write_view(root, {"sol": SOL_ID, "opus": OPUS_ID})
    _reset_aliases()
    yield root
    _reset_aliases()


def _config(root, sol_alias):
    # Inherited lane is claude-alr on purpose: the alias, not the profile, must name cpa.
    (root / "config.yaml").write_text(yaml.safe_dump({
        "model": {"default": "opus", "provider": "claude-alr", "aliases": {"sol": sol_alias}},
        "providers": _LANES,
    }), encoding="utf-8")
    _reset_aliases()


@pytest.fixture(params=sorted(_ALIAS_FORMS))
def aliased(request, vhome):
    _config(vhome, _ALIAS_FORMS[request.param])
    return vhome


@pytest.fixture
def literal(vhome):
    _config(vhome, f"cpa/{SOL_ID}")
    return vhome


def _turn(model, provider):
    """What the agent sends: init resolves the stored slot (#466 apply_slot_at_init)."""
    from agent.model_slots import apply_slot_at_init

    agent = SimpleNamespace(model=model, provider=provider)
    apply_slot_at_init(agent)
    return agent.provider, agent.model


def _switch(raw, current="claude-alr", probe=False):
    from hermes_cli.model_switch import switch_model

    return switch_model(raw, current, OPUS_ID, current_base_url=_LANES[current]["base_url"],
                        current_api_key="fake-test-token", user_providers=_LANES, probe_catalog=probe)


def _catalog_only(*ids):
    """A live-listing validator that accepts exactly ``ids`` (records what it was asked)."""
    seen = []

    def _validate(model, provider, **_kw):
        seen.append(model)
        ok = model in ids
        return {"accepted": ok, "persist": ok, "recognized": ok,
                "message": None if ok else f"`{model}` not found on {provider}"}

    return _validate, seen


# ── (a) /model sol ─────────────────────────────────────────────────────────────

def test_a_model_command_stores_the_slot_on_the_alias_lane(aliased):
    from hermes_cli.model_switch import format_switch_target

    res = _switch("sol")
    assert res.success, res.error_message
    assert (res.target_provider, res.new_model) == ("cpa", "sol")
    assert format_switch_target(res.new_model) == f"sol -> {SOL_ID}"
    assert _turn(res.new_model, res.target_provider) == ("cpa", SOL_ID)
    _write_view(aliased, {"sol": SOL_NEXT})
    assert _turn(res.new_model, res.target_provider) == ("cpa", SOL_NEXT)


def test_a_e_model_command_validates_the_resolved_id(aliased):
    validate, seen = _catalog_only(SOL_ID)
    with patch("hermes_cli.models._validate_requested_model_seam", return_value=validate):
        res = _switch("sol", probe=True)
    assert res.success, res.error_message
    assert (res.target_provider, res.new_model) == ("cpa", "sol")
    assert seen == [SOL_ID]


def test_a_e_slot_is_not_normalized_before_it_resolves(vhome, monkeypatch):
    # Prism 83fa1f7f219b: openrouter's normalizer turned slot ``gemini`` into ``google/gemini``,
    # and slot_resolve kept that prefix -> probe ``google/google/gemini-2.5-pro``.
    monkeypatch.setenv("OPENROUTER_API_KEY", "fake-test-token")
    _write_view(vhome, {"gemini": "google/gemini-2.5-pro"})
    (vhome / "config.yaml").write_text(yaml.safe_dump({
        "model": {"default": "opus", "provider": "claude-alr",
                  "aliases": {"gemini": "openrouter/gemini"}},
        "providers": _LANES,
    }), encoding="utf-8")
    _reset_aliases()
    validate, seen = _catalog_only("google/gemini-2.5-pro")
    with patch("hermes_cli.models._validate_requested_model_seam", return_value=validate):
        res = _switch("gemini", probe=True)
    assert res.success, res.error_message
    assert (res.target_provider, res.new_model) == ("openrouter", "gemini")
    assert seen == ["google/gemini-2.5-pro"]
    assert _turn(res.new_model, res.target_provider) == ("openrouter", "google/gemini-2.5-pro")


# ── (b) hermes -m sol at CLI startup ───────────────────────────────────────────

def _startup(raw):
    """cli_init_mixin's order: resolve_startup_model_route, then resolve_startup_model_arg."""
    from hermes_cli.model_switch import resolve_startup_model_arg, resolve_startup_model_route

    route = resolve_startup_model_route(raw, current_provider="claude-alr", user_providers=_LANES)
    if route is not None:
        return route.provider, route.model
    return resolve_startup_model_arg(raw, "claude-alr", _LANES)


def test_b_startup_m_runs_the_slot_on_the_alias_lane_and_follows_a_flip(aliased):
    from hermes_cli.model_switch import resolve_startup_model_arg

    assert _startup("sol") == ("cpa", "sol")
    assert resolve_startup_model_arg("sol", "claude-alr", _LANES) == ("cpa", "sol")
    provider, model = _startup("sol")
    assert _turn(model, provider) == ("cpa", SOL_ID)
    _write_view(aliased, {"sol": SOL_NEXT})
    provider, model = _startup("sol")  # the next start
    assert _turn(model, provider) == ("cpa", SOL_NEXT)


# ── (c) kanban pin and cron job ────────────────────────────────────────────────

def _served_only(monkeypatch, *ids):
    """Registry catalog: the lane serves exactly ``ids``; record what it was asked."""
    from hermes_cli import model_registry

    seen = []
    monkeypatch.setattr(model_registry, "catalog_for", lambda provider, *a, **k: (seen.append(provider), ids)[1])
    return seen


def test_c_kanban_pin_is_stored_as_the_slot_and_dispatched_as_the_id(aliased, monkeypatch):
    from hermes_cli import kanban_db as kb

    _served_only(monkeypatch, SOL_ID)
    kb.init_db()
    with kb.connect() as conn:
        created = kb.create_task(conn, title="pin", assignee="worker", model_override="sol")
        edited = kb.create_task(conn, title="edit", assignee="worker")
        assert kb.set_task_model(conn, edited, "sol") == 1
        overridden = kb.create_task(conn, title="set-model", assignee="worker")
        kb.set_model_override(conn, overridden, "sol", None)
    with kb.connect() as conn:
        tasks = [kb.get_task(conn, t) for t in (created, edited, overridden)]
    for task in tasks:
        assert (task.model_override, task.provider_override) == ("sol", "cpa"), task.title

    # The dispatcher passes the pair through as ``-m <model_override> --provider <provider_override>``
    # (kanban_db_dispatch worker argv); the worker's startup resolves that, then the agent the slot.
    from hermes_cli.model_switch import resolve_startup_model_route

    route = resolve_startup_model_route("sol", explicit_provider="cpa", current_provider="claude-alr",
                                        user_providers=_LANES)
    worker = (route.provider, route.model) if route is not None else ("cpa", "sol")
    assert worker == ("cpa", "sol")
    assert _turn(worker[1], worker[0]) == ("cpa", SOL_ID)
    _write_view(aliased, {"sol": SOL_NEXT})
    assert _turn(worker[1], worker[0]) == ("cpa", SOL_NEXT)


def test_c_cron_job_is_stored_as_the_slot_and_fires_as_the_id(aliased, monkeypatch):
    from cron import jobs as cj
    from cron.scheduler import _load_cron_job_config

    _served_only(monkeypatch, SOL_ID)
    job = cj.create_job(prompt="ping", schedule="in 1 hour", model="sol")
    assert (job["model"], job["provider"]) == ("sol", "cpa")
    stored = [j for j in cj.load_jobs() if j["id"] == job["id"]][0]
    assert (stored["model"], stored["provider"]) == ("sol", "cpa")
    updated = cj.update_job(job["id"], {"model": "sol"})
    assert (updated["model"], updated["provider"]) == ("sol", "cpa")
    with patch("cron.scheduler._get_hermes_home", return_value=aliased):
        assert _load_cron_job_config(stored, job["id"], "job").model == SOL_ID
        _write_view(aliased, {"sol": SOL_NEXT})
        assert _load_cron_job_config(stored, job["id"], "job").model == SOL_NEXT


# ── (d) delegate_task(model='sol') ─────────────────────────────────────────────

def test_d_delegate_runs_the_id_on_the_alias_lane(aliased, monkeypatch):
    from tools import delegate_tool
    from hermes_cli import model_registry

    seen = []

    def _require(provider, model, **_kw):
        seen.append((provider, model))
        raise ValueError("stop after admission")

    monkeypatch.setattr(model_registry, "require", _require)
    out = json.loads(delegate_tool.delegate_task(goal="x", model="sol", parent_agent=MagicMock(provider="claude-alr")))
    assert "stop after admission" in out["error"]
    assert seen == [("cpa", SOL_ID)]


# ── (e) write-time validation sees the resolved id ─────────────────────────────

def test_e_writers_validate_the_resolved_id_not_the_slot(aliased, monkeypatch):
    from cron import jobs as cj
    from hermes_cli import kanban_db as kb
    from hermes_cli.model_registry import UnknownModel

    _served_only(monkeypatch, SOL_ID)
    kb.init_db()
    with kb.connect() as conn:
        kb.create_task(conn, title="ok", assignee="worker", model_override="sol")
    cj.create_job(prompt="ping", schedule="in 1 hour", model="sol")
    # the lane stops serving the slot's id: the slot is refused, as its id would be
    _served_only(monkeypatch, "gpt-0-other")
    with kb.connect() as conn, pytest.raises((UnknownModel, ValueError)):
        kb.create_task(conn, title="bad", assignee="worker", model_override="sol")
    with pytest.raises((UnknownModel, ValueError)):
        cj.create_job(prompt="ping", schedule="in 1 hour", model="sol")


# ── (f) literal aliases are unchanged ──────────────────────────────────────────

def test_f_literal_alias_behaves_as_on_main(literal, monkeypatch):
    from cron import jobs as cj
    from hermes_cli import kanban_db as kb
    from hermes_cli.model_switch import resolve_model_pair_for_storage

    assert resolve_model_pair_for_storage("sol") == (SOL_ID, "cpa")
    assert _startup("sol") == ("cpa", SOL_ID)
    res = _switch("sol")
    assert res.success, res.error_message
    assert (res.target_provider, res.new_model) == ("cpa", SOL_ID)
    _served_only(monkeypatch, SOL_ID)
    kb.init_db()
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="lit", assignee="worker", model_override="sol")
        task = kb.get_task(conn, tid)
    # P6 (t_efde9d1a): the alias resolves to the slot's CURRENT id, which a writer now stores as
    # the slot on the alias lane; the run still sends the id.
    assert (task.model_override, task.provider_override) == ("sol", "cpa")
    job = cj.create_job(prompt="ping", schedule="in 1 hour", model="sol")
    assert (job["model"], job["provider"]) == ("sol", "cpa")
    assert _turn("sol", "cpa") == ("cpa", SOL_ID)


def test_bare_slot_with_no_alias_stays_on_the_current_lane(vhome):
    # No alias, no rule: a bare slot is a bare model name on the current lane (today's behaviour).
    (vhome / "config.yaml").write_text(yaml.safe_dump({
        "model": {"default": "opus", "provider": "claude-alr"}, "providers": _LANES}), encoding="utf-8")
    _reset_aliases()
    from hermes_cli.model_switch import resolve_model_pair_for_storage

    from hermes_cli.model_switch import resolve_startup_model_arg

    assert resolve_model_pair_for_storage("sol") == ("sol", None)
    assert resolve_startup_model_arg("sol", "claude-alr", _LANES) == (None, "sol")
    assert _turn("sol", "claude-alr") == ("claude-alr", SOL_ID)


# ── carried from #469: aux seams that read config model.default directly ──────

def test_custom_endpoint_aux_sends_the_slots_id(vhome):
    from agent import auxiliary_client as aux

    with patch.object(aux, "_resolve_custom_runtime",
                      return_value=("http://127.0.0.1:9999/v1", "fake-test-token", None)), \
         patch.object(aux, "_read_main_model_for_aux", return_value="sol"), \
         patch.object(aux, "_create_openai_client", return_value=MagicMock()):
        assert aux._try_custom_endpoint()[1] == SOL_ID


def test_custom_endpoint_aux_literal_and_no_view_unchanged(vhome):
    from agent import auxiliary_client as aux

    (vhome / "config" / "model-slots.json").unlink()
    with patch.object(aux, "_resolve_custom_runtime",
                      return_value=("http://127.0.0.1:9999/v1", "fake-test-token", None)), \
         patch.object(aux, "_create_openai_client", return_value=MagicMock()):
        with patch.object(aux, "_read_main_model_for_aux", return_value="sol"):
            assert aux._try_custom_endpoint()[1] == "sol"
        with patch.object(aux, "_read_main_model_for_aux", return_value=SOL_ID):
            assert aux._try_custom_endpoint()[1] == SOL_ID


def test_vision_auto_route_checks_the_slots_id(vhome):
    from agent import auxiliary_client as aux

    seen = []

    def _main_client(provider, model, *a, **k):
        seen.append(model)
        return None, None

    with patch.object(aux, "_read_main_model", return_value="sol"), \
         patch.object(aux, "_vision_main_provider_client", side_effect=_main_client), \
         patch.object(aux, "_resolve_strict_vision_backend", return_value=(None, None)):
        aux._vision_auto_route({"provider": "cpa"}, None, None, False)
    assert seen == [SOL_ID]


# ── carried from #469: slot_view on a torn or vanishing file ──────────────────

def test_torn_view_keeps_the_last_good_parse(vhome):
    from hermes_cli.model_registry import slot_resolve

    assert slot_resolve("sol") == (SOL_ID, "sol")
    path = vhome / "config" / "model-slots.json"
    path.write_text('{"slots": {"sol": "gpt-6', encoding="utf-8")  # mid-rewrite
    st = path.stat()
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))
    assert slot_resolve("sol") == (SOL_ID, "sol")
    _write_view(vhome, {"sol": SOL_NEXT})
    assert slot_resolve("sol") == (SOL_NEXT, "sol")
    path.unlink()
    assert slot_resolve("sol") == ("sol", None)


def test_view_deleted_between_stat_and_read_is_missing(vhome):
    from hermes_cli.model_registry import slot_resolve

    assert slot_resolve("sol") == (SOL_ID, "sol")
    path = vhome / "config" / "model-slots.json"
    st = path.stat()
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 7_000_000_000))
    real = Path.read_text

    def _gone(self, *a, **k):
        if self == path:
            raise FileNotFoundError(str(path))
        return real(self, *a, **k)

    with patch.object(Path, "read_text", _gone):
        assert slot_resolve("sol") == ("sol", None)


def test_view_deleted_then_torn_recreate_does_not_revive_the_old_mapping(vhome):
    # Prism a8cda9522c00: a missing file must drop the cached view, or a torn re-create falls
    # back to it and routes to the deleted mapping.
    from hermes_cli.model_registry import slot_resolve

    assert slot_resolve("sol") == (SOL_ID, "sol")
    path = vhome / "config" / "model-slots.json"
    path.unlink()
    assert slot_resolve("sol") == ("sol", None)
    path.write_text('{"slots": {"sol": "gpt-6', encoding="utf-8")  # re-created, mid-write
    assert slot_resolve("sol") == ("sol", None)


def test_view_deleted_between_stat_and_read_then_torn_does_not_revive(vhome):
    from hermes_cli.model_registry import slot_resolve

    assert slot_resolve("sol") == (SOL_ID, "sol")
    path = vhome / "config" / "model-slots.json"
    st = path.stat()
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 7_000_000_000))
    real = Path.read_text

    def _gone(self, *a, **k):
        if self == path:
            raise FileNotFoundError(str(path))
        return real(self, *a, **k)

    with patch.object(Path, "read_text", _gone):
        assert slot_resolve("sol") == ("sol", None)
    path.write_text('{"slots": {"sol": "gpt-6', encoding="utf-8")
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 9_000_000_000))
    assert slot_resolve("sol") == ("sol", None)


# ── aggregator lanes (openrouter): the id sent is the id validated ───────────────
# Real view ids carry no vendor prefix (kimi -> kimi-k3.1-pro); openrouter needs one on the
# wire. Apollo send-back on f69ad85c: head probed moonshotai/kimi-k3.1-pro but sent kimi-k3.1-pro.

_OR_URL = "https://openrouter.ai/api/v1"
_OR_CASES = {
    "k3-or": ("kimi", "kimi-k3.1-pro", "moonshotai/kimi-k3.1-pro"),
    "gemini-pro": ("gemini-pro", "gemini-3.1-pro", "google/gemini-3.1-pro"),
    "grok-fast": ("grok-fast", "grok-4.5-fast", "x-ai/grok-4.5-fast"),
}


def _real_agent(model, provider, base_url):
    from run_agent import AIAgent

    cfg = {"model": {"default": model, "provider": provider}}
    with (
        patch("hermes_cli.config.load_config", return_value=cfg),
        patch("hermes_cli.config.load_config_readonly", return_value=cfg),
        patch("model_tools.get_tool_definitions", return_value=[]),
        patch("model_tools.check_toolset_requirements", return_value={}),
        patch("agent.process_bootstrap.OpenAI"),
    ):
        return AIAgent(model=model, provider=provider, api_key="fake-test-token", base_url=base_url,
                       quiet_mode=True, skip_context_files=True, skip_memory=True)


def _wire(agent):
    return agent._build_api_kwargs([{"role": "user", "content": "hi"}])["model"]


@pytest.mark.parametrize("alias", sorted(_OR_CASES))
def test_aggregator_slot_sends_the_id_it_validated(vhome, monkeypatch, alias):
    slot, view_id, wire_id = _OR_CASES[alias]
    monkeypatch.setenv("OPENROUTER_API_KEY", "fake-test-token")
    _write_view(vhome, {slot: view_id, "opus": OPUS_ID})
    (vhome / "config.yaml").write_text(yaml.safe_dump({
        "model": {"default": "opus", "provider": "claude-alr", "aliases": {alias: f"openrouter/{slot}"}},
        "providers": _LANES,
    }), encoding="utf-8")
    _reset_aliases()
    validate, seen = _catalog_only(wire_id)
    with patch("hermes_cli.models._validate_requested_model_seam", return_value=validate):
        res = _switch(alias, probe=True)
    assert res.success, res.error_message
    assert (res.target_provider, res.new_model) == ("openrouter", slot)
    assert seen == [wire_id]

    # A fresh agent on the stored route (next start, kanban/cron spawn).
    fresh = _real_agent(res.new_model, res.target_provider, _OR_URL)
    assert _wire(fresh) == wire_id

    # A live agent switched in place (gateway/CLI /model), then a flip at turn start.
    from agent.agent_runtime_helpers import switch_model as live_switch
    from agent.model_slots import refresh_slot_for_turn

    live = _real_agent(OPUS_ID, "claude-alr", _LANES["claude-alr"]["base_url"])
    live_switch(live, res.new_model, res.target_provider, api_key="fake-test-token", base_url=_OR_URL,
                api_mode="chat_completions", persist_billing_route=False, persist_fallback=False)
    assert _wire(live) == wire_id
    assert refresh_slot_for_turn(live) is False  # unchanged view: no churn
    _write_view(vhome, {slot: view_id + "-next", "opus": OPUS_ID})
    assert refresh_slot_for_turn(live) is True
    assert _wire(live) == wire_id + "-next"
    assert _wire(_real_agent(res.new_model, res.target_provider, _OR_URL)) == wire_id + "-next"


# The reader's probe (Apollo send-back on 44ac374f), as a test: the real view's prefix-less ids,
# one openrouter alias per spelling the sweep writes. probed == sent == the vendor-prefixed id,
# stored == the bare slot.
_REAL_VIEW = {"kimi": "kimi-k3.1-pro", "gemini-pro": "gemini-4-argon", "opus": OPUS_ID}
_OR_PROBE = {
    "k3-or": ("openrouter/kimi", "kimi", "moonshotai/kimi-k3.1-pro"),
    "gp-or": ({"provider": "openrouter", "model": "gemini-pro"}, "gemini-pro", "google/gemini-4-argon"),
}


@pytest.mark.parametrize("alias", sorted(_OR_PROBE))
def test_openrouter_alias_probes_sends_and_stores_per_the_contract(vhome, monkeypatch, alias):
    _value, slot, wire_id = _OR_PROBE[alias]
    monkeypatch.setenv("OPENROUTER_API_KEY", "fake-test-token")
    _write_view(vhome, _REAL_VIEW)
    (vhome / "config.yaml").write_text(yaml.safe_dump({
        "model": {"default": "opus", "provider": "claude-alr",
                  "aliases": {name: v for name, (v, _s, _w) in _OR_PROBE.items()}},
        "providers": _LANES,
    }), encoding="utf-8")
    _reset_aliases()
    validate, seen = _catalog_only(wire_id)
    with patch("hermes_cli.models._validate_requested_model_seam", return_value=validate):
        res = _switch(alias, probe=True)
    assert res.success, res.error_message
    assert (res.target_provider, res.new_model) == ("openrouter", slot)
    assert seen == [wire_id]
    assert _wire(_real_agent(res.new_model, res.target_provider, _OR_URL)) == wire_id


def test_slot_resolve_does_not_double_a_vendor_prefix_the_target_carries(vhome):
    from hermes_cli.model_registry import slot_resolve

    _write_view(vhome, {"gemini": "google/gemini-2.5-pro"})
    assert slot_resolve("google/gemini") == ("google/gemini-2.5-pro", "google/gemini")
    assert slot_resolve("openrouter/gemini") == ("openrouter/google/gemini-2.5-pro", "openrouter/gemini")


def test_gateway_does_not_evict_an_aggregator_slot_default_every_turn(vhome):
    # The cached agent now runs moonshotai/kimi-k3.1-pro; the config default ``kimi`` must compare
    # as that id, or every gateway turn evicts the agent (and its prompt cache).
    from gateway.run_turn import GatewayTurnMixin

    _write_view(vhome, {"kimi": "kimi-k3.1-pro"})
    evicted = []
    gw = SimpleNamespace(_is_intentional_model_switch=lambda *a: False,
                         _evict_cached_agent=evicted.append)
    agent = SimpleNamespace(model="moonshotai/kimi-k3.1-pro", provider="openrouter")
    ctx = SimpleNamespace(session_key="s1", agent_holder=[agent], result_holder=[{}])
    with patch("gateway.run._resolve_gateway_model", return_value="kimi"):
        GatewayTurnMixin._run_agent_evict_on_fallback(gw, ctx)
    assert evicted == []
    agent.model = "moonshotai/kimi-k3"  # a different model on the agent still evicts
    with patch("gateway.run._resolve_gateway_model", return_value="kimi"):
        GatewayTurnMixin._run_agent_evict_on_fallback(gw, ctx)
    assert evicted == ["s1"]
