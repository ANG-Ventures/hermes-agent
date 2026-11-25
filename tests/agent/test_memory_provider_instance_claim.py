"""One memory-provider instance serves one profile (#140058, #146293).

Ported from upstream tests/agent/test_memory_provider_caller_profile.py (419e6627aa, d0154fe03b,
c9de60702c), instance-claim tests only. The last tests drive the real agent build path with the
real mem0 provider for two profiles in one process: each must keep a working provider.
"""
import gc
import json
import logging
import urllib.request
import weakref
from types import SimpleNamespace
from urllib.parse import urlparse

import pytest

from agent.memory_manager import MemoryManager
from agent.memory_provider import MemoryProvider
from hermes_constants import reset_hermes_home_override, set_hermes_home_override

_TOOL = "ct_remember"
_REFUSAL = "memory_provider_instance_shared"


class _Scope:
    def __init__(self, home):
        self.home = home

    def __enter__(self):
        self.token = set_hermes_home_override(str(self.home))

    def __exit__(self, *exc):
        reset_hermes_home_override(self.token)


class _InstanceScopedProvider(MemoryProvider):
    """Meets the provider contract: its store binding lives on the instance, set from
    ``initialize``'s ``hermes_home``."""

    events: list = []

    def __init__(self):
        self._home = None

    @property
    def name(self):
        return "instancescoped"

    def is_available(self):
        return True

    def initialize(self, session_id, **kwargs):
        self._home = kwargs["hermes_home"]

    def get_tool_schemas(self):
        return [{"name": _TOOL, "description": "remember", "parameters": {"type": "object", "properties": {}}}]

    def handle_tool_call(self, tool_name, args, **kwargs):
        type(self).events.append(self._home)
        return json.dumps({"ok": True})


class _UnhashableProvider(_InstanceScopedProvider):
    __hash__ = None


class _ValueEqualProvider(_InstanceScopedProvider):
    """Every instance compares (and hashes) equal to every other: equality is not identity."""

    def __eq__(self, other):
        return isinstance(other, _ValueEqualProvider)

    def __hash__(self):
        return 0


@pytest.fixture(autouse=True)
def _fresh_events():
    _InstanceScopedProvider.events = []
    yield


@pytest.fixture
def homes(tmp_path):
    root = tmp_path / ".hermes"
    alpha, beta = root / "profiles" / "alpha", root / "profiles" / "beta"
    for home in (alpha, beta):
        home.mkdir(parents=True)
    return alpha, beta


@pytest.fixture
def multiplexed():
    from agent.secret_scope import reset_multiplex_context, set_multiplex_context
    token = set_multiplex_context(True)
    yield
    reset_multiplex_context(token)


def _manager(home, provider=None):
    manager = MemoryManager()
    with _Scope(home):
        manager.add_provider(provider or _InstanceScopedProvider())
        manager.initialize_all(session_id=f"s-{home.name}", platform="api_server", hermes_home=str(home))
    return manager


def _logged(caplog):
    return "\n".join(r.getMessage() for r in caplog.records if r.name == "agent.memory_caller_scope")


@pytest.mark.parametrize("mux", [False, True], ids=["multiplex-off", "multiplex-on"])
def test_one_provider_instance_is_never_served_to_a_second_profile(homes, caplog, mux, request):
    if mux:
        request.getfixturevalue("multiplexed")
    alpha_home, beta_home = homes
    shared = _InstanceScopedProvider()
    alpha = _manager(alpha_home, shared)
    beta = _manager(beta_home, shared)
    # The first owner keeps the instance and its binding; the second got a refusal, not a rebind.
    assert shared._home == str(alpha_home)
    assert alpha.providers == [shared] and beta.providers == []
    assert not beta.has_tool(_TOOL) and alpha.has_tool(_TOOL)
    with _Scope(alpha_home):
        assert json.loads(alpha.handle_tool_call(_TOOL, {"content": "alpha private note"})) == {"ok": True}
    assert _InstanceScopedProvider.events == [str(alpha_home)]
    logged = _logged(caplog)
    assert _REFUSAL in logged and str(beta_home) in logged, logged


def test_same_profile_may_reuse_its_own_instance(homes):
    alpha_home, _ = homes
    shared = _InstanceScopedProvider()
    first, second = _manager(alpha_home, shared), _manager(alpha_home, shared)
    assert first.providers == [shared] and second.providers == [shared]


def test_distinct_instances_each_serve_their_profile(homes):
    # The guard refuses a shared INSTANCE, not multi-profile use: two instances of one class are fine.
    alpha_home, beta_home = homes
    first, second = _InstanceScopedProvider(), _InstanceScopedProvider()
    alpha, beta = _manager(alpha_home, first), _manager(beta_home, second)
    assert (first._home, second._home) == (str(alpha_home), str(beta_home))
    assert alpha.providers == [first] and beta.providers == [second]


def test_unhashable_shared_provider_is_refused_for_a_second_profile(homes):
    alpha_home, beta_home = homes
    shared = _UnhashableProvider()
    alpha, beta = _manager(alpha_home, shared), _manager(beta_home, shared)
    assert shared._home == str(alpha_home)
    assert alpha.providers and beta.providers == []


def test_value_equal_distinct_instances_are_each_served(homes):
    alpha_home, beta_home = homes
    first, second = _ValueEqualProvider(), _ValueEqualProvider()
    alpha, beta = _manager(alpha_home, first), _manager(beta_home, second)
    assert (first._home, second._home) == (str(alpha_home), str(beta_home))
    assert alpha.providers == [first] and beta.providers == [second]


def test_claim_is_released_when_the_instance_dies(homes):
    from agent.memory_caller_scope import _claims, claim_instance
    alpha_home, beta_home = homes
    provider = _InstanceScopedProvider()
    assert claim_instance(provider, alpha_home)
    pid = id(provider)
    assert pid in _claims
    del provider
    gc.collect()
    assert pid not in _claims


def test_untrackable_provider_instance_fails_closed_only_when_multiplexed(homes):
    from agent.memory_caller_scope import claim_instance
    from agent.secret_scope import reset_multiplex_context, set_multiplex_context

    class _NoWeakref:
        __slots__ = ("name",)

    alpha_home, _ = homes
    assert claim_instance(_NoWeakref(), alpha_home)
    token = set_multiplex_context(True)
    try:
        assert not claim_instance(_NoWeakref(), alpha_home)
    finally:
        reset_multiplex_context(token)


def test_nonweakref_shared_provider_keeps_first_home_while_its_managers_live(homes, caplog):
    class _SlotsProvider:
        __slots__ = ("bound_home",)
        name = "slots_only"

        def __init__(self):
            self.bound_home = None

        def is_available(self):
            return True

        def initialize(self, session_id, **kwargs):
            self.bound_home = kwargs["hermes_home"]

        def get_tool_schemas(self):
            return [{"name": _TOOL, "description": "remember", "parameters": {"type": "object", "properties": {}}}]

        def handle_tool_call(self, tool_name, args, **kwargs):
            return json.dumps({"store_home": self.bound_home})

    alpha_home, beta_home = homes
    shared = _SlotsProvider()
    with pytest.raises(TypeError):
        weakref.ref(shared)

    first = _manager(alpha_home, shared)
    second_same_home = _manager(alpha_home, shared)
    refused = _manager(beta_home, shared)

    assert first.providers == [shared] and second_same_home.providers == [shared]
    assert refused.providers == [] and not refused.has_tool(_TOOL)
    assert shared.bound_home == str(alpha_home)
    logged = _logged(caplog)
    assert _REFUSAL in logged and str(beta_home) in logged, logged

    # The claim is held by alpha's live managers, not by the provider: once they are gone, beta may bind it.
    del first, second_same_home
    gc.collect()
    rebound = _manager(beta_home, shared)
    assert rebound.providers == [shared] and shared.bound_home == str(beta_home)


def test_initialize_all_records_the_bound_scope(homes):
    from hermes_constants import hermes_home_key
    alpha_home, _ = homes
    manager = MemoryManager()
    manager.add_provider(_InstanceScopedProvider())
    manager.initialize_all(session_id="s", hermes_home=str(alpha_home), agent_identity="alpha")
    assert manager._bound_scope == {
        "hermes_home": str(alpha_home), "agent_identity": "alpha", "home_key": hermes_home_key(alpha_home)}


def test_initialize_all_looks_claim_instance_up_through_the_module_global(homes, monkeypatch):
    # hermes-webui wraps ``agent.memory_manager.claim_instance`` (install_shared_provider_instance_guard).
    import agent.memory_manager as mm
    seen = []

    def _refuse(provider, home, **kwargs):
        seen.append((provider.name, str(home), kwargs.get("manager") is not None))
        return False

    monkeypatch.setattr(mm, "claim_instance", _refuse)
    alpha_home, _ = homes
    provider = _InstanceScopedProvider()
    manager = MemoryManager()
    manager.add_provider(provider)
    assert manager.initialize_for_agent(session_id="s", hermes_home=str(alpha_home)) is False
    assert seen == [("instancescoped", str(alpha_home), True)]
    assert manager.providers == [] and provider._home is None  # refused BEFORE initialize ran


# -- The real agent build path ------------------------------------------------------------------------


class _ProfileTurn(_Scope):
    """A served profile's turn: its HERMES_HOME override plus the secret scope a multiplexed gateway binds."""

    def __enter__(self):
        from agent.secret_scope import build_profile_secret_scope, set_secret_scope
        super().__enter__()
        self.secret_token = set_secret_scope(build_profile_secret_scope(self.home), profile_home=str(self.home))

    def __exit__(self, *exc):
        from agent.secret_scope import reset_secret_scope
        reset_secret_scope(self.secret_token)
        super().__exit__(*exc)


def _stub_agent(session_id):
    agent = SimpleNamespace(
        enabled_toolsets=None, disabled_toolsets=None, session_id=session_id, _session_db=None,
        session_cwd=None, _emit_warning=lambda *_: None, _emit_status=lambda *_: None,
    )
    from agent.agent_init import _GATEWAY_IDENTITY_PARAMS
    for ident in _GATEWAY_IDENTITY_PARAMS:
        setattr(agent, f"_{ident}", None)
    return agent


@pytest.fixture
def no_tool_injection(monkeypatch):
    import agent.memory_manager as mm
    monkeypatch.setattr(mm, "inject_memory_provider_tools", lambda agent: None)


def test_agent_whose_provider_instance_is_refused_runs_without_it(homes, monkeypatch, caplog, no_tool_injection):
    from agent import agent_init
    alpha_home, beta_home = homes
    shared = _InstanceScopedProvider()
    _manager(alpha_home, shared)
    monkeypatch.setattr("plugins.memory.load_memory_provider", lambda name: shared)
    agent = _stub_agent("s-beta")
    with _Scope(beta_home), caplog.at_level(logging.DEBUG):
        agent_init._init_memory(agent, {"memory": {"provider": "instancescoped"}}, False, "api_server")
    assert agent._memory_manager is None
    assert not any("activated" in r.getMessage() for r in caplog.records)
    assert any("is disabled for this profile" in r.getMessage() for r in caplog.records)
    assert shared._home == str(alpha_home)


@pytest.mark.parametrize("mux", [False, True], ids=["multiplex-off", "multiplex-on"])
def test_two_profiles_each_get_a_working_mem0_provider(homes, monkeypatch, mux, request, no_tool_injection):
    """The fleet shape: mem0 configured per profile, two profiles built in one process (WebUI, or a
    multiplexed gateway). Both must keep mem0, and each search must reach its own profile's server."""
    pytest.importorskip("mem0")
    if mux:
        request.getfixturevalue("multiplexed")
    from agent import agent_init
    for var in ("MEM0_API_KEY", "MEM0_HOST", "MEM0_ADMIN_API_KEY", "MEM0_USER_ID", "MEM0_AGENT_ID"):
        monkeypatch.delenv(var, raising=False)
    for home in homes:
        (home / "mem0.json").write_text(json.dumps({
            "host": f"http://mem0-{home.name}.test", "admin_api_key": f"key-{home.name}",
            "user_id": f"user-{home.name}", "agent_id": home.name,
        }))
    calls = []

    class _Resp:
        def __init__(self, payload):
            self._payload = payload

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return json.dumps(self._payload).encode("utf-8")

    def fake_urlopen(req, timeout=0, context=None):
        parsed = urlparse(req.full_url)
        body = json.loads(req.data.decode("utf-8")) if req.data else {}
        calls.append((parsed.netloc, parsed.path, body.get("user_id")))
        if parsed.path == "/search":
            return _Resp({"results": [{"id": "m", "memory": f"fact from {parsed.netloc}", "score": 0.9}]})
        return _Resp({"results": []})

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    agents = {}
    for home in homes:
        agent = _stub_agent(f"s-{home.name}")
        with _ProfileTurn(home):
            agent_init._init_memory(agent, {"memory": {"provider": "mem0"}}, False, "api_server")
        agents[home.name] = agent
        if agent._memory_manager is not None:  # shut down even when a later build or assert fails
            request.addfinalizer(agent._memory_manager.shutdown_all)

    alpha, beta = agents["alpha"]._memory_manager, agents["beta"]._memory_manager
    assert alpha is not None and beta is not None
    (pa,), (pb,) = alpha.providers, beta.providers
    assert pa.name == pb.name == "mem0" and pa is not pb
    assert alpha.has_tool("mem0_search") and beta.has_tool("mem0_search")
    for name, manager in (("alpha", alpha), ("beta", beta)):
        home = next(h for h in homes if h.name == name)
        with _ProfileTurn(home):
            result = json.loads(manager.handle_tool_call("mem0_search", {"query": "needle"}))
        assert result["count"] == 1, result
        assert result["results"][0]["memory"] == f"fact from mem0-{name}.test"
    searches = [c for c in calls if c[1] == "/search"]
    assert searches == [("mem0-alpha.test", "/search", "user-alpha"), ("mem0-beta.test", "/search", "user-beta")]
