"""Slot aliases P2b (t_2f4dc291): every seat that takes a model resolves a slot name.

hermes-agent#466 resolves a slot only for the MAIN seat (agent.model). This file covers the other seat
classes through the same ``hermes_cli.model_registry.slot_resolve`` seam: pricing and reprice, context
metadata, fallback rungs, auxiliary seats, delegation, MoA, x_search, cron, kanban pins and the /model
confirmation text. Each class also checks that a literal id and a missing view behave as before.

Measured 2026-11-19 on the Studio: a turn sent as ``cpa/sol`` was recorded ``provider=cpa model=sol``
with cost NULL, and tokens-reprice-sweep paged ``unpriced model cpa/sol``.
"""
import json
import os
from unittest.mock import MagicMock, patch

import pytest

SOL_ID = "gpt-6.2-sol"
OPUS_ID = "claude-opus-5-5"


def _write_view(root, slots):
    path = root / "config" / "model-slots.json"
    path.write_text(json.dumps({"rendered_from": "abc123", "slots": dict(slots)}), encoding="utf-8")
    st = path.stat()
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))


@pytest.fixture
def home(tmp_path, monkeypatch):
    root = tmp_path / "vhome"
    (root / "config").mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(root))
    _write_view(root, {"sol": SOL_ID, "opus": OPUS_ID})
    return root


@pytest.fixture
def no_view(home):
    (home / "config" / "model-slots.json").unlink()
    return home


# ── pricing: live cost and the reprice sweep ───────────────────────────────────

def test_billing_route_prices_a_slot_as_its_id(home):
    from agent.usage_pricing import get_pricing_entry, resolve_billing_route

    assert resolve_billing_route("sol", provider="cpa") == resolve_billing_route(SOL_ID, provider="cpa")
    assert get_pricing_entry("sol", provider="cpa") is not None
    assert get_pricing_entry("sol", provider="cpa") == get_pricing_entry(SOL_ID, provider="cpa")
    # lane-prefixed form, as the sweep's uid spells it
    assert get_pricing_entry("cpa/sol", provider="cpa") == get_pricing_entry(SOL_ID, provider="cpa")


def test_billing_route_without_view_is_unchanged(no_view):
    from agent.usage_pricing import get_pricing_entry, resolve_billing_route

    assert resolve_billing_route("sol", provider="cpa").model == "sol"
    assert get_pricing_entry("sol", provider="cpa") is None
    assert get_pricing_entry(SOL_ID, provider="cpa") is not None


def test_compute_turn_cost_slot_equals_id(home):
    from plugins.blackbox.cost import compute_turn_cost

    calls = [dict(input_tokens=1000, output_tokens=200, cache_read_tokens=0, cache_write_tokens=0)]
    slot = compute_turn_cost("sol", "cpa", None, calls)
    literal = compute_turn_cost(SOL_ID, "cpa", None, calls)
    assert literal[1] != "unknown"
    assert slot == literal


def _seed_turns(rows):
    from plugins.blackbox import store
    from plugins.blackbox.record import TurnRecord

    conn = store._connect()
    conn.close()
    for turn_id, model in rows:
        store.insert_turn(TurnRecord(turn_id=turn_id, provider="cpa", model=model,
                                     input_tokens=1000, output_tokens=200))
    return store


def _reprice(store):
    from plugins.blackbox.cost import compute_turn_cost

    def fn(m, p, t):  # the tokens-reprice-sweep driver, verbatim shape
        return compute_turn_cost(m, p, None, [dict(
            input_tokens=t["input_tokens"], output_tokens=t["output_tokens"],
            cache_read_tokens=t["cache_read_tokens"], cache_write_tokens=t["cache_write_tokens"])])

    return store.reprice_unpriced(fn, apply=True)


def _costs(store):
    import sqlite3

    con = sqlite3.connect(str(store._db_path()))
    try:
        return dict(con.execute("SELECT turn_id, cost_usd FROM turns").fetchall())
    finally:
        con.close()


def test_reprice_heals_a_turn_recorded_as_cpa_sol(home):
    store = _seed_turns([("slot-turn", "sol"), ("id-turn", SOL_ID)])
    # Recorded unpriced, as on the Studio: wipe whatever insert-time pricing wrote.
    import sqlite3
    con = sqlite3.connect(str(store._db_path()))
    con.execute("UPDATE turns SET cost_usd=NULL, cost_status=NULL, cost_uncached_usd=NULL, "
                "cost_cache_read_usd=NULL, cost_cache_write_usd=NULL, cost_output_usd=NULL")
    con.commit()
    con.close()

    res = _reprice(store)
    costs = _costs(store)
    assert costs["id-turn"] is not None and costs["id-turn"] > 0
    assert costs["slot-turn"] == costs["id-turn"], res
    assert res["still_unknown"] == 0


def test_reprice_without_view_leaves_the_slot_turn_unpriced(no_view):
    store = _seed_turns([("slot-turn", "sol")])
    import sqlite3
    con = sqlite3.connect(str(store._db_path()))
    con.execute("UPDATE turns SET cost_usd=NULL, cost_status=NULL, cost_uncached_usd=NULL, "
                "cost_cache_read_usd=NULL, cost_cache_write_usd=NULL, cost_output_usd=NULL")
    con.commit()
    con.close()
    res = _reprice(store)
    assert _costs(store)["slot-turn"] is None
    assert res["still_unknown"] == 1


# ── context metadata: any seat's lookup ───────────────────────────────────────

def test_context_length_of_a_slot_is_its_ids(home):
    from agent.model_metadata import get_model_context_length

    assert get_model_context_length("sol", provider="cpa") == get_model_context_length(SOL_ID, provider="cpa")


# ── fallback rungs ─────────────────────────────────────────────────────────────

def _fb_agent(chain):
    from run_agent import AIAgent

    with (
        patch("model_tools.get_tool_definitions", return_value=[]),
        patch("model_tools.check_toolset_requirements", return_value={}),
        patch("agent.process_bootstrap.OpenAI"),
    ):
        agent = AIAgent(api_key="test-key", base_url="https://openrouter.ai/api/v1", quiet_mode=True,
                        skip_context_files=True, skip_memory=True, fallback_model=chain)
        agent.client = MagicMock()
        return agent


def _activate(agent):
    client = MagicMock()
    client.base_url = "http://127.0.0.1:8317/v1"
    client.api_key = "fb-key"
    with (
        patch("agent.chat_completion_helpers._fallback_entry_unavailable_without_network", return_value=None),
        patch("agent.auxiliary_client.resolve_provider_client",
              side_effect=lambda p, model=None, **kw: (client, model)) as rpc,
        patch("hermes_cli.model_normalize.normalize_model_for_provider", side_effect=lambda m, p: m),
    ):
        assert agent._try_activate_fallback() is True
    return rpc


def test_fallback_rung_named_by_slot_runs_on_the_id(home):
    agent = _fb_agent([{"provider": "cpa", "model": "sol", "base_url": "http://127.0.0.1:8317/v1"}])
    rpc = _activate(agent)
    assert agent.model == SOL_ID
    assert rpc.call_args.kwargs["model"] == SOL_ID


def test_fallback_rung_literal_unchanged(home):
    agent = _fb_agent([{"provider": "cpa", "model": "gpt-6.1-terra", "base_url": "http://127.0.0.1:8317/v1"}])
    _activate(agent)
    assert agent.model == "gpt-6.1-terra"


# ── auxiliary seats (vision, compression, titles, goal_judge, approval, ...) ──

@pytest.mark.parametrize("task", ["compression", "vision", "title_generation", "goal_judge", "approval"])
def test_aux_task_config_slot_resolves(home, task):
    from agent import auxiliary_client as aux

    with patch.object(aux, "_get_auxiliary_task_config", return_value={"provider": "cpa", "model": "sol"}):
        provider, model, *_ = aux._resolve_task_provider_model(task)
    assert (provider, model) == ("cpa", SOL_ID)


def test_aux_explicit_model_kwarg_slot_resolves(home):
    from agent import auxiliary_client as aux

    with patch.object(aux, "_get_auxiliary_task_config", return_value={}):
        _, model, *_ = aux._resolve_task_provider_model("moa_reference", provider="cpa", model="opus")
    assert model == OPUS_ID


def test_aux_literal_and_missing_view_unchanged(no_view):
    from agent import auxiliary_client as aux

    with patch.object(aux, "_get_auxiliary_task_config", return_value={"provider": "cpa", "model": "sol"}):
        assert aux._resolve_task_provider_model("compression")[1] == "sol"
    with patch.object(aux, "_get_auxiliary_task_config", return_value={"provider": "cpa", "model": SOL_ID}):
        assert aux._resolve_task_provider_model("compression")[1] == SOL_ID


def test_resolve_provider_client_resolves_before_the_branch(home, monkeypatch):
    """The client factory every aux/fallback/MoA path ends in sees the id, never the slot."""
    from agent import auxiliary_client as aux

    seen = {}
    monkeypatch.setattr(aux, "_resolve_named_custom_branch", lambda req: None)
    monkeypatch.setattr(aux, "_resolve_registry_branch", lambda req: seen.setdefault("m", req.model) and (None, req.model))
    monkeypatch.setattr(aux, "_EXPLICIT_PROVIDER_BRANCHES", {})
    _, model = aux.resolve_provider_client("cpa", model="sol")
    assert seen["m"] == SOL_ID and model == SOL_ID


# ── delegation / delegate_task ─────────────────────────────────────────────────

def test_delegation_credentials_resolve_a_slot(home):
    from tools.delegate_tool_config import _credential_bundle

    assert _credential_bundle("sol", "cpa", None, None, None, None)["model"] == SOL_ID
    assert _credential_bundle("cpa/sol", "cpa", None, None, None, None)["model"] == f"cpa/{SOL_ID}"
    assert _credential_bundle(SOL_ID, "cpa", None, None, None, None)["model"] == SOL_ID
    assert _credential_bundle(None, None, None, None, None, None)["model"] is None


# ── MoA ────────────────────────────────────────────────────────────────────────

def test_moa_member_slot_resolves(home):
    from agent import moa_loop

    moa_loop._runtime_cache.clear()
    with patch("hermes_cli.runtime_provider.resolve_runtime_provider", side_effect=RuntimeError("offline")):
        assert moa_loop._slot_runtime({"provider": "cpa", "model": "opus"})["model"] == OPUS_ID
        assert moa_loop._slot_runtime({"provider": "cpa", "model": "gpt-6.1-terra"})["model"] == "gpt-6.1-terra"


# ── x_search ───────────────────────────────────────────────────────────────────

def test_x_search_model_slot_resolves(home, monkeypatch):
    from tools import x_search_tool

    _write_view(home, {"grok-search": "grok-4.20-reasoning"})
    captured = {}

    class _Resp:
        status_code = 200
        text = "{}"

        def raise_for_status(self):
            return None

        def json(self):
            return {"output_text": "ok", "citations": [{"url": "https://x.com/a/status/1"}]}

    def _post(url, headers=None, json=None, timeout=None):
        captured["json"] = json
        return _Resp()

    monkeypatch.setenv("XAI_API_KEY", "xai-test-key")
    monkeypatch.setattr("requests.post", _post)
    monkeypatch.setattr(x_search_tool, "_load_x_search_config", lambda: {"model": "grok-search"})
    x_search_tool.x_search_tool(query="what is new")
    assert captured["json"]["model"] == "grok-4.20-reasoning"


# ── cron job model ─────────────────────────────────────────────────────────────

def test_cron_job_model_slot_resolves(home):
    from cron.scheduler import _load_cron_job_config

    (home / "config.yaml").write_text("model:\n  default: gpt-6.1-terra\n", encoding="utf-8")
    with patch("cron.scheduler._get_hermes_home", return_value=home):
        assert _load_cron_job_config({"model": "sol"}, "j1", "job").model == SOL_ID
        assert _load_cron_job_config({"model": "cpa/sol"}, "j1", "job").model == f"cpa/{SOL_ID}"
        assert _load_cron_job_config({"model": "gpt-6.1-terra"}, "j1", "job").model == "gpt-6.1-terra"
        # no job pin: the profile default, literal here
        assert _load_cron_job_config({}, "j1", "job").model == "gpt-6.1-terra"


# ── kanban model_override ──────────────────────────────────────────────────────

def test_kanban_pin_validation_checks_the_slots_id(home, monkeypatch):
    from hermes_cli import model_registry

    seen = []

    def _resolve(provider, model, resolve_aliases=True):
        seen.append(model)
        return MagicMock(ok=model == SOL_ID, model=model, provider=provider)

    monkeypatch.setattr(model_registry, "resolve", _resolve)
    assert model_registry.invalid_pin_reason("cpa", "sol") is None
    assert seen == [SOL_ID]
    assert model_registry.invalid_pin_reason("cpa", "gpt-0-bogus") is not None


def test_kanban_card_pin_compares_slot_and_id_equal(home):
    from hermes_cli.kanban_worker_route import _model_key

    assert _model_key("sol") == _model_key(SOL_ID)
    assert _model_key("gpt-6.1-terra") != _model_key(SOL_ID)


def test_kanban_shim_cap_ranks_a_slot_card(home):
    from hermes_cli.kanban_db import _shim_model_rank

    assert _shim_model_rank("opus") == _shim_model_rank(OPUS_ID) == 3


def test_writers_check_a_slot_as_its_id(home, monkeypatch):
    """kanban create/set-model, cron create/edit and delegate_task all call require(): a slot is
    admitted when its id is served, and the writer keeps storing the slot."""
    from hermes_cli import model_registry

    seen = []

    def _resolve(provider, model, resolve_aliases=True, force_reason=None):
        seen.append(model)
        return MagicMock(ok=model == SOL_ID, model=model, provider=provider, valid_ids=(SOL_ID,))

    monkeypatch.setattr(model_registry, "resolve", _resolve)
    model_registry.require("cpa", "sol")
    assert seen == [SOL_ID]
    with pytest.raises(model_registry.UnknownModel):
        model_registry.require("cpa", "gpt-0-bogus")


def test_flagship_ban_sees_through_a_slot(home):
    from hermes_cli.model_policy import flagship_model_match

    cfg = {"kanban": {"banned_worker_model_substrings": ["claude-opus-5-5"]}}
    with patch("hermes_cli.model_policy.banned_worker_model_substrings", return_value=("claude-opus-5-5",)):
        assert flagship_model_match("opus", cfg) == "claude-opus-5-5"
        assert flagship_model_match("sol", cfg) is None
        assert flagship_model_match(OPUS_ID, cfg) == "claude-opus-5-5"


# ── /model confirmation ────────────────────────────────────────────────────────

def test_model_switch_confirmation_names_slot_and_id(home):
    from hermes_cli.model_switch import format_switch_target

    assert format_switch_target("sol") == f"sol -> {SOL_ID}"
    assert format_switch_target(SOL_ID) == SOL_ID


def test_model_switch_confirmation_without_view(no_view):
    from hermes_cli.model_switch import format_switch_target

    assert format_switch_target("sol") == "sol"


# ── P2c (t_81e582e0): a slot name wins over an identical alias ─────────────────
# The Studio's model.aliases carry slot-named keys (sol, opus, ...). Resolved at write time they
# stored the literal id, so a pinned card/cron/`/model sol` never followed a slot flip.

_STALE_SOL = "gpt-6.1-sol"
_LANES = {
    "cpa": {"base_url": "http://127.0.0.1:8317/v1", "api_key": "fake-test-token"},
    "claude-alr": {"base_url": "http://127.0.0.1:8318/v1", "api_key": "fake-test-token"},
}


@pytest.fixture
def aliased(home):
    """Slot-named aliases (one stale on purpose), a non-slot alias, and a slot with no alias."""
    import yaml

    _write_view(home, {"sol": SOL_ID, "opus": OPUS_ID, "kimi": "kimi-k3.1-pro", "grok": "grok-4.7"})
    (home / "config.yaml").write_text(yaml.safe_dump({
        "model": {
            "default": "sol", "provider": "cpa",
            "aliases": {"sol": f"cpa/{_STALE_SOL}", "opus": f"claude-alr/{OPUS_ID}",
                        "grok": "xai-oauth/grok-4.6",
                        "k31": "cpa/kimi-k3.1-pro", "sol-e": f"cpa/{SOL_ID}"},
        },
        "providers": _LANES,
    }), encoding="utf-8")
    return home


def test_storage_keeps_a_slot_named_alias_as_the_slot_on_the_aliases_lane(aliased):
    from hermes_cli.model_switch import resolve_model_pair_for_storage as store

    assert store("sol") == ("sol", "cpa")
    assert store("opus") == ("opus", "claude-alr")
    # inline lane + slot-named alias on that lane: the slot is kept, not the alias's id
    assert store("xai-oauth/grok") == ("grok", "xai-oauth")
    assert store("xai-oauth:grok") == ("grok", "xai-oauth")
    # a lane prefix the parser does not know stays on the string (slot_resolve keeps it)
    assert store("cpa/sol") == ("cpa/sol", None)
    # explicit provider still wins over the alias lane
    assert store("sol", "claude-alr") == ("sol", "claude-alr")


def test_storage_leaves_non_slot_aliases_and_literals_alone(aliased):
    from hermes_cli.model_switch import resolve_model_pair_for_storage as store

    assert store("k31") == ("kimi-k3.1-pro", "cpa")
    assert store("sol-e") == (SOL_ID, "cpa")
    assert store(SOL_ID) == (SOL_ID, None)


def test_storage_without_view_is_unchanged(aliased):
    from hermes_cli.model_switch import resolve_model_pair_for_storage as store

    (aliased / "config" / "model-slots.json").unlink()
    assert store("sol") == (_STALE_SOL, "cpa")
    assert store("opus") == (OPUS_ID, "claude-alr")
    assert store("xai-oauth/grok") == ("grok-4.6", "xai-oauth")


def _switch(raw, current="cpa"):
    from hermes_cli.model_switch import switch_model

    return switch_model(raw, current, "claude-haiku-5-5", current_base_url=_LANES[current]["base_url"],
                        current_api_key="fake-test-token", user_providers=_LANES, probe_catalog=False)


def test_model_command_stores_the_slot_and_keeps_the_alias_lane(aliased):
    from hermes_cli.model_switch import format_switch_target

    res = _switch("sol")
    assert res.success, res.error_message
    assert (res.new_model, res.target_provider) == ("sol", "cpa")
    assert format_switch_target(res.new_model) == f"sol -> {SOL_ID}"

    res = _switch("opus")  # alias lane claude-alr, typed from cpa
    assert res.success, res.error_message
    assert (res.new_model, res.target_provider) == ("opus", "claude-alr")

    res = _switch("cpa/sol", current="claude-alr")  # lane-pinned stays on its lane
    assert res.success, res.error_message
    assert (res.new_model, res.target_provider) == ("sol", "cpa")


def test_model_command_slot_wins_over_the_builtin_family_catalog(aliased):
    # `kimi` is a MODEL_ALIASES family and has no user alias: the slot decides, no catalog search.
    with patch("hermes_cli.model_switch.list_provider_models",
               side_effect=AssertionError("catalog consulted for a slot")):
        res = _switch("kimi")
    assert res.success, res.error_message
    assert (res.new_model, res.target_provider) == ("kimi", "cpa")


def test_model_command_non_slot_alias_and_literal_unchanged(aliased):
    res = _switch("k31")
    assert res.success, res.error_message
    assert (res.new_model, res.target_provider) == ("kimi-k3.1-pro", "cpa")
    res = _switch(SOL_ID)
    assert res.success, res.error_message
    assert res.new_model == SOL_ID


def test_model_command_without_view_is_unchanged(aliased):
    (aliased / "config" / "model-slots.json").unlink()
    res = _switch("sol")
    assert res.success, res.error_message
    assert (res.new_model, res.target_provider) == (_STALE_SOL, "cpa")


# ── P2c: aux seams that read config model.default directly ────────────────────

def test_custom_endpoint_aux_sends_the_slots_id(home):
    from agent import auxiliary_client as aux

    with patch.object(aux, "_resolve_custom_runtime",
                      return_value=("http://127.0.0.1:9999/v1", "fake-test-token", None)), \
         patch.object(aux, "_read_main_model_for_aux", return_value="sol"), \
         patch.object(aux, "_create_openai_client", return_value=MagicMock()):
        _client, model = aux._try_custom_endpoint()
    assert model == SOL_ID


def test_custom_endpoint_aux_literal_and_no_view_unchanged(no_view):
    from agent import auxiliary_client as aux

    with patch.object(aux, "_resolve_custom_runtime",
                      return_value=("http://127.0.0.1:9999/v1", "fake-test-token", None)), \
         patch.object(aux, "_create_openai_client", return_value=MagicMock()):
        with patch.object(aux, "_read_main_model_for_aux", return_value="sol"):
            assert aux._try_custom_endpoint()[1] == "sol"
        with patch.object(aux, "_read_main_model_for_aux", return_value=SOL_ID):
            assert aux._try_custom_endpoint()[1] == SOL_ID


def test_vision_auto_route_checks_the_slots_id(home):
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


# ── P2c: a torn view keeps the last good parse ─────────────────────────────────

def test_torn_view_keeps_the_last_good_parse(home):
    from hermes_cli.model_registry import slot_resolve

    assert slot_resolve("sol") == (SOL_ID, "sol")
    path = home / "config" / "model-slots.json"
    path.write_text('{"slots": {"sol": "gpt-6', encoding="utf-8")  # mid-rewrite
    st = path.stat()
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 2_000_000_000))
    assert slot_resolve("sol") == (SOL_ID, "sol")
    # a later good render is picked up
    _write_view(home, {"sol": "gpt-6.3-sol"})
    assert slot_resolve("sol") == ("gpt-6.3-sol", "sol")
    # a missing file is still pass-through
    path.unlink()
    assert slot_resolve("sol") == ("sol", None)
