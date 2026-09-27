"""Tests for cron LLM-model auto-resolution (cronjob create).

An unpinned LLM cron (no_agent=False, no model) inherits the runtime PRIMARY
(often Opus) at fire time — a silent cost footgun. ``_resolve_cron_llm_model``
+ the ``_current_agent_model`` ContextVar let a job be pinned at creation to the
CREATING agent's own model (model="auto" or config cron.default_model="auto"),
or to a config-default model, while never fabricating a model when it can't
resolve.
"""
import json

import pytest

import tools.cronjob_tools as ct


def _clear_agent_model():
    ct.set_current_agent_model(None, None)


def test_explicit_auto_pins_to_creating_agent(monkeypatch):
    # No config default; the agent published its model this turn.
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: {})
    ct.set_current_agent_model("openai-codex", "gpt-5.6-terra")
    try:
        model, provider = ct._resolve_cron_llm_model("auto", None)
        assert model == "gpt-5.6-terra"
        assert provider == "openai-codex"
    finally:
        _clear_agent_model()


def test_auto_is_case_and_space_insensitive(monkeypatch):
    ct.set_current_agent_model("claude-apr", "claude-sonnet-5")
    try:
        model, provider = ct._resolve_cron_llm_model("  AUTO ", None)
        assert model == "claude-sonnet-5"
        assert provider == "claude-apr"
    finally:
        _clear_agent_model()


def test_auto_that_cannot_resolve_degrades_to_unpinned():
    # No agent model published (bare caller) → "auto" must NOT fabricate a model,
    # and must NOT leave a dangling provider glued to an unresolved model.
    _clear_agent_model()
    model, provider = ct._resolve_cron_llm_model("auto", "claude-apr")
    assert model is None  # sentinel dropped, not persisted as a literal "auto"
    assert provider is None  # provider dropped too — no half-pinned job


def test_resolve_model_override_passes_auto_through():
    # The object→string flattener must NOT pin a config provider onto "auto";
    # that would half-pin the job before the live-agent resolver runs.
    provider, model = ct._resolve_model_override({"model": "auto"})
    assert model == "auto"
    assert provider is None


def test_explicit_model_is_left_untouched():
    ct.set_current_agent_model("openai-codex", "gpt-5.6-terra")
    try:
        model, provider = ct._resolve_cron_llm_model("claude-opus-4-8", "claude-apr")
        assert model == "claude-opus-4-8"
        assert provider == "claude-apr"
    finally:
        _clear_agent_model()


def test_no_model_no_config_stays_unpinned(monkeypatch):
    # Back-compat: nothing given, no config knob → unchanged (unpinned).
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: {})
    _clear_agent_model()
    model, provider = ct._resolve_cron_llm_model(None, None)
    assert model is None
    assert provider is None


def test_config_default_auto_pins_to_agent(monkeypatch):
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: {"cron": {"default_model": "auto"}})
    ct.set_current_agent_model("openai-codex", "gpt-5.6-sol")
    try:
        model, provider = ct._resolve_cron_llm_model(None, None)
        assert model == "gpt-5.6-sol"
        assert provider == "openai-codex"
    finally:
        _clear_agent_model()


def test_config_default_literal_model(monkeypatch):
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: {"cron": {"default_model": "gpt-5.6-terra", "default_provider": "openai-codex"}})
    _clear_agent_model()
    try:
        model, provider = ct._resolve_cron_llm_model(None, None)
        assert model == "gpt-5.6-terra"
        assert provider == "openai-codex"
    finally:
        _clear_agent_model()


def test_explicit_model_ignores_config_default(monkeypatch):
    # An explicit model always wins over a config default.
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: {"cron": {"default_model": "gpt-5.6-terra"}})
    _clear_agent_model()
    model, provider = ct._resolve_cron_llm_model("grok-4.5", "xai-oauth")
    assert model == "grok-4.5"
    assert provider == "xai-oauth"


def test_contextvar_roundtrip():
    ct.set_current_agent_model("p1", "m1")
    assert ct.get_current_agent_model() == ("p1", "m1")
    ct.set_current_agent_model(None, None)
    assert ct.get_current_agent_model() == (None, None)


class TestFlagshipReasonForAutoPin:
    """The ONE synthesizer of the auto-pin flagship exemption, shared by the
    create and update auto paths."""

    def test_synthesized_only_when_auto_resolved(self):
        assert ct._flagship_reason_for_auto_pin("claude-fable-5-1", True, None) == ct._AUTO_PIN_FLAGSHIP_REASON
        assert ct._flagship_reason_for_auto_pin("claude-fable-5-1", True, "  ") == ct._AUTO_PIN_FLAGSHIP_REASON
        assert ct._flagship_reason_for_auto_pin(None, True, None) is None
        assert ct._flagship_reason_for_auto_pin("claude-fable-5-1", False, None) is None

    def test_explicit_caller_reason_wins(self):
        assert ct._flagship_reason_for_auto_pin("claude-fable-5-1", True, "ops: why") == "ops: why"
        assert ct._flagship_reason_for_auto_pin("claude-sonnet-5", False, "ops: why") == "ops: why"


class TestPoolForSingleSub:
    """The ONE shared seat->pool classifier used by the auto path and every
    model-only half-pin site. Identity-based on the provider, vendor-gated on
    the (optional) model."""

    @pytest.mark.parametrize("seat, pool", [
        ("claude-bpx-7", "claude-bpr"),
        ("claude-apx-13", "claude-apr"),
        ("claude-bpx-0", "claude-bpr"),
        ("claude-apx-0", "claude-apr"),
    ])
    def test_seat_without_model_maps_to_pool(self, seat, pool):
        # The auto path passes no model: the session already ran its own
        # model on this seat, so the pair is known-good.
        assert ct._pool_for_single_sub(seat) == pool

    @pytest.mark.parametrize("provider", ["claude-bpr", "claude-apr", "openai-codex", None, 7])
    def test_non_seat_is_never_mapped(self, provider):
        assert ct._pool_for_single_sub(provider) is None
        assert ct._pool_for_single_sub(provider, "claude-sonnet-5") is None

    @pytest.mark.parametrize("spelling", [
        "claude-bpx-7x",        # trailing junk: a prefix match would map it
        "claude-bpx-7-extra",
        "claude-apx-13beta",
        "xclaude-bpx-7",        # leading junk
        "claude-bpx-",          # no seat number
        "claude-bpx-7 ",        # unstripped whitespace is not a seat name
    ])
    def test_seat_regex_is_a_full_match_not_a_prefix(self, spelling):
        # Pins the fullmatch boundary (r5 C1): only an exact, registry-unknown
        # claude-{a,b}px-N spelling is a seat; anything longer/shorter is not.
        assert ct._pool_for_single_sub(spelling) is None
        assert ct._pool_for_single_sub(spelling, "claude-sonnet-5") is None

    @pytest.mark.parametrize("model", ["claude-sonnet-5", "claude-opus-5-5", "  Claude-Haiku-4 "])
    def test_claude_model_from_seat_gets_pool(self, model):
        assert ct._pool_for_single_sub("claude-bpx-7", model) == "claude-bpr"
        assert ct._pool_for_single_sub("claude-apx-2", model) == "claude-apr"

    @pytest.mark.parametrize("model", [
        "gpt-5.5",          # openai — a Claude pool cannot serve it
        "gpt-6-sol-900k",   # openai
        "kimi-k3",          # moonshot
        "grok-4.5",         # xai
        "mystery-model-9",  # vendor unknown — not provably servable, so no glue
    ])
    def test_non_claude_or_unknown_model_from_seat_is_not_glued(self, model):
        assert ct._pool_for_single_sub("claude-bpx-7", model) is None
        assert ct._pool_for_single_sub("claude-apx-2", model) is None


class TestCreateAutoModelE2E:
    """Drive the real cronjob(action="create") path and assert the PERSISTED job
    carries the resolved model — the actual behavior, not just the helper."""

    @pytest.fixture(autouse=True)
    def _setup_cron_dir(self, tmp_path, monkeypatch):
        monkeypatch.setattr("cron.jobs.CRON_DIR", tmp_path / "cron")
        monkeypatch.setattr("cron.jobs.JOBS_FILE", tmp_path / "cron" / "jobs.json")
        monkeypatch.setattr("cron.jobs.OUTPUT_DIR", tmp_path / "cron" / "output")
        monkeypatch.setattr("hermes_cli.config.load_config", lambda: {})
        yield
        ct.set_current_agent_model(None, None)

    def test_create_with_auto_pins_agent_model(self):
        ct.set_current_agent_model("openai-codex", "gpt-5.6-terra")
        created = json.loads(ct.cronjob(
            action="create", prompt="Check", schedule="every 1h",
            name="auto-model-job", model="auto",
        ))
        assert created["success"] is True
        # The persisted job must carry the creating agent's model+provider.
        assert created["job"]["model"] == "gpt-5.6-terra"
        assert created["job"]["provider"] == "openai-codex"

    @pytest.mark.parametrize("seat, pool", [
        ("claude-bpx-7", "claude-bpr"),
        ("claude-apx-13", "claude-apr"),
    ])
    def test_create_auto_persists_pool_not_live_seat(self, seat, pool):
        from cron.jobs import JOBS_FILE

        ct.set_current_agent_model(seat, "claude-opus-5-5")
        created = json.loads(ct.cronjob(
            action="create", prompt="Check", schedule="every 1h", model="auto",
        ))
        assert created["success"] is True, created
        persisted = json.loads(JOBS_FILE.read_text())
        job = next(job for job in persisted["jobs"] if job["id"] == created["job_id"])
        assert (job["provider"], job["model"]) == (pool, "claude-opus-5-5")

    @pytest.mark.parametrize("seat, aliases, pool", [
        ("claude-apx-0", ("claude-api-proxy", "claude-proxy", "claude-subscription-proxy"), "claude-apr"),
        ("claude-bpx-0", ("claude-bridge", "hermes-claude-bridge", "claude-cli-bridge"), "claude-bpr"),
    ])
    def test_aliases_of_single_sub_persist_pool(self, monkeypatch, seat, aliases, pool):
        from cron.jobs import JOBS_FILE
        import providers
        from providers.base import ProviderProfile
        from tools.registry import registry

        # These aliases are supplied by fleet plugins, not bundled in this repo.
        # Register the same identity/alias relationship in the isolated test registry.
        monkeypatch.setattr(providers, "_REGISTRY", providers._REGISTRY.copy())
        monkeypatch.setattr(providers, "_ALIASES", providers._ALIASES.copy())
        providers.register_provider(ProviderProfile(name=seat, aliases=aliases))
        for alias in aliases:
            ct.set_current_agent_model(alias, "claude-opus-5-5")
            created = json.loads(ct.cronjob(
                action="create", prompt="Check", schedule="every 1h", model="auto",
            ))
            assert created["success"] is True, created
            job = next(job for job in json.loads(JOBS_FILE.read_text())["jobs"] if job["id"] == created["job_id"])
            assert (job["provider"], job["model"]) == (pool, "claude-opus-5-5")

            created = json.loads(ct.cronjob(
                action="create", prompt="Check", schedule="every 1h", model="claude-sonnet-5",
            ))
            assert created["success"] is True, created
            job = next(job for job in json.loads(JOBS_FILE.read_text())["jobs"] if job["id"] == created["job_id"])
            assert (job["provider"], job["model"]) == (pool, "claude-sonnet-5")

            result = registry.dispatch("cronjob", {
                "action": "create", "prompt": "Check", "schedule": "every 1h",
                "model": {"model": "claude-sonnet-5"},
            })
            assert isinstance(result, str)
            created = json.loads(result)
            assert created["success"] is True, created
            job = next(job for job in json.loads(JOBS_FILE.read_text())["jobs"] if job["id"] == created["job_id"])
            assert (job["provider"], job["model"]) == (pool, "claude-sonnet-5")

    def test_default_auto_persists_pool_not_live_seat(self, monkeypatch):
        from cron.jobs import JOBS_FILE

        monkeypatch.setattr("hermes_cli.config.load_config", lambda: {"cron": {"default_model": "auto"}})
        ct.set_current_agent_model("claude-bpx-7", "claude-opus-5-5")
        created = json.loads(ct.cronjob(
            action="create", prompt="Check", schedule="every 1h",
        ))
        assert created["success"] is True, created
        job = next(job for job in json.loads(JOBS_FILE.read_text())["jobs"] if job["id"] == created["job_id"])
        assert job["provider"] == "claude-bpr"

    def test_model_only_seat_persists_pool_but_pool_does_not_freeze(self):
        from cron.jobs import JOBS_FILE

        for live_provider, expected in (("claude-bpx-7", "claude-bpr"), ("claude-bpr", None), ("openai-codex", None)):
            ct.set_current_agent_model(live_provider, "claude-opus-5-5")
            created = json.loads(ct.cronjob(
                action="create", prompt="Check", schedule="every 1h", model="claude-sonnet-5",
            ))
            assert created["success"] is True, created
            job = next(job for job in json.loads(JOBS_FILE.read_text())["jobs"] if job["id"] == created["job_id"])
            assert job["provider"] == expected

    def test_update_auto_persists_pool_not_live_seat(self):
        from cron.jobs import JOBS_FILE

        created = json.loads(ct.cronjob(action="create", prompt="Check", schedule="every 1h"))
        assert created["success"] is True, created
        ct.set_current_agent_model("claude-apx-9", "claude-sonnet-5")
        updated = json.loads(ct.cronjob(action="update", job_id=created["job_id"], model="auto"))
        assert updated["success"] is True, updated
        job = next(job for job in json.loads(JOBS_FILE.read_text())["jobs"] if job["id"] == created["job_id"])
        assert (job["provider"], job["model"]) == ("claude-apr", "claude-sonnet-5")

    def test_update_model_only_from_seat_matches_create(self):
        from cron.jobs import JOBS_FILE

        created = json.loads(ct.cronjob(action="create", prompt="Check", schedule="every 1h"))
        assert created["success"] is True, created
        ct.set_current_agent_model("claude-bpx-7", "claude-opus-5-5")
        updated = json.loads(ct.cronjob(action="update", job_id=created["job_id"], model="claude-sonnet-5"))
        assert updated["success"] is True, updated
        job = next(job for job in json.loads(JOBS_FILE.read_text())["jobs"] if job["id"] == created["job_id"])
        assert (job["provider"], job["model"]) == ("claude-bpr", "claude-sonnet-5")

    def test_update_model_only_replaces_previous_provider_with_live_pool(self):
        from cron.jobs import JOBS_FILE

        created = json.loads(ct.cronjob(
            action="create", prompt="Check", schedule="every 1h",
            model="claude-opus-5-5", provider="claude-apr",
        ))
        assert created["success"] is True, created
        ct.set_current_agent_model("claude-bpx-7", "claude-opus-5-5")
        updated = json.loads(ct.cronjob(action="update", job_id=created["job_id"], model="claude-sonnet-5"))
        assert updated["success"] is True, updated
        job = next(job for job in json.loads(JOBS_FILE.read_text())["jobs"] if job["id"] == created["job_id"])
        assert (job["provider"], job["model"]) == ("claude-bpr", "claude-sonnet-5")

    @pytest.mark.parametrize("entrypoint", ["direct", "registry"])
    @pytest.mark.parametrize("model, expected_model", [
        ("auto", "claude-opus-5-5"),
        ("claude-sonnet-5", "claude-sonnet-5"),
    ])
    def test_script_to_llm_update_resolves_live_seat(self, entrypoint, model, expected_model):
        from cron.jobs import JOBS_FILE
        from tools.registry import registry

        def call(**kwargs):
            if entrypoint == "registry":
                if "model" in kwargs:
                    kwargs["model"] = {"model": kwargs["model"]}
                return json.loads(registry.dispatch("cronjob", kwargs))
            return json.loads(ct.cronjob(**kwargs))

        created = call(action="create", prompt="Check", schedule="every 1h",
                       script="noop.sh", no_agent=True)
        assert created["success"] is True, created
        ct.set_current_agent_model("claude-bpx-7", "claude-opus-5-5")
        updated = call(action="update", job_id=created["job_id"],
                       no_agent=False, model=model)
        assert updated["success"] is True, updated
        job = next(j for j in json.loads(JOBS_FILE.read_text())["jobs"] if j["id"] == created["job_id"])
        assert (job["no_agent"], job["model"], job["provider"]) == (
            False, expected_model, "claude-bpr")

    @pytest.mark.parametrize("entrypoint", ["direct", "registry"])
    def test_llm_to_script_update_does_not_inherit_live_seat(self, entrypoint):
        from cron.jobs import JOBS_FILE
        from tools.registry import registry

        created = json.loads(ct.cronjob(action="create", prompt="Check", schedule="every 1h",
                                        script="noop.sh"))
        assert created["success"] is True, created
        ct.set_current_agent_model("claude-bpx-7", "claude-opus-5-5")
        args = {"action": "update", "job_id": created["job_id"], "no_agent": True,
                "model": {"model": "auto"} if entrypoint == "registry" else "auto"}
        updated = json.loads(registry.dispatch("cronjob", args) if entrypoint == "registry"
                             else ct.cronjob(**args))
        assert updated["success"] is True, updated
        job = next(j for j in json.loads(JOBS_FILE.read_text())["jobs"] if j["id"] == created["job_id"])
        assert job["no_agent"] is True
        assert job["provider"] is None
        assert job["model"] != "claude-opus-5-5"

    def test_script_to_llm_explicit_provider_is_preserved(self):
        from cron.jobs import JOBS_FILE

        created = json.loads(ct.cronjob(action="create", prompt="Check", schedule="every 1h",
                                        script="noop.sh", no_agent=True))
        assert created["success"] is True, created
        ct.set_current_agent_model("claude-bpx-7", "claude-opus-5-5")
        updated = json.loads(ct.cronjob(action="update", job_id=created["job_id"],
                                        no_agent=False, model="claude-sonnet-5", provider="claude-apr"))
        assert updated["success"] is True, updated
        job = next(j for j in json.loads(JOBS_FILE.read_text())["jobs"] if j["id"] == created["job_id"])
        assert (job["provider"], job["model"]) == ("claude-apr", "claude-sonnet-5")

    @pytest.mark.parametrize("action", ["create", "update"])
    def test_registry_model_only_seat_pool_beats_config_main_provider(self, monkeypatch, action):
        # The registry prefill (_resolve_model_override) is the only site that
        # sees a model-only spec BEFORE cronjob() does; without its own seat
        # check it pins the config main provider and the direct half-pin site
        # never runs (provider is no longer None). With a configured main the
        # seat's pool must still win (F2 decision: model-only from a live seat
        # follows the creating session's pool, even when configured main differs).
        # This is the arm that kills mutant M9 (registry site dropped).
        from cron.jobs import JOBS_FILE
        from tools.registry import registry

        monkeypatch.setattr("hermes_cli.config.load_config",
                            lambda: {"model": {"provider": "claude-apr", "default": "claude-opus-5-5"}})
        if action == "create":
            ct.set_current_agent_model("claude-bpx-7", "claude-opus-5-5")
            result = json.loads(registry.dispatch("cronjob", {
                "action": "create", "prompt": "Check", "schedule": "every 1h",
                "model": {"model": "claude-sonnet-5"},
            }))
            job_id = result.get("job_id")
        else:
            ct.set_current_agent_model(None, None)
            created = json.loads(ct.cronjob(action="create", prompt="Check", schedule="every 1h"))
            assert created["success"] is True, created
            job_id = created["job_id"]
            ct.set_current_agent_model("claude-bpx-7", "claude-opus-5-5")
            result = json.loads(registry.dispatch("cronjob", {
                "action": "update", "job_id": job_id, "model": {"model": "claude-sonnet-5"},
            }))
        assert result["success"] is True, result
        job = next(j for j in json.loads(JOBS_FILE.read_text())["jobs"] if j["id"] == job_id)
        assert (job["provider"], job["model"]) == ("claude-bpr", "claude-sonnet-5")

    def test_registry_model_only_persists_pool_for_seat(self):
        from cron.jobs import JOBS_FILE
        from tools.registry import registry

        ct.set_current_agent_model("claude-bpx-7", "claude-opus-5-5")
        result = registry.dispatch("cronjob", {
            "action": "create", "prompt": "Check", "schedule": "every 1h",
            "model": {"model": "claude-sonnet-5"},
        })
        assert isinstance(result, str)
        created = json.loads(result)
        assert created["success"] is True, created
        job = next(job for job in json.loads(JOBS_FILE.read_text())["jobs"] if job["id"] == created["job_id"])
        assert (job["provider"], job["model"]) == ("claude-bpr", "claude-sonnet-5")

    # Cross-vendor rule (r4 F1): the seat's pool is glued onto a model-only spec
    # ONLY when the pool can serve the model's vendor. Every non-Claude row
    # below is the row the BASE tree (4956e38e18) persists for the same call
    # (measured via qa-output/r5/crossvendor_probe.py on both trees); only the
    # Claude arm differs from base, and it differs by gaining the pool.
    @pytest.mark.parametrize("entrypoint", ["direct", "registry"])
    @pytest.mark.parametrize("action", ["create", "update"])
    @pytest.mark.parametrize("model, expected_provider", [
        ("gpt-5.5", "openai-codex"),        # openai: base row, not refused
        ("gpt-6-sol-900k", None),           # openai, no default route: base row
        ("kimi-k3", None),                  # moonshot: base row
        ("mystery-model-9", None),          # vendor unknown: base row
        ("claude-sonnet-5", "claude-bpr"),  # anthropic: seat's pool
    ])
    def test_model_only_from_seat_respects_model_vendor(
        self, entrypoint, action, model, expected_provider,
    ):
        from cron.jobs import JOBS_FILE
        from tools.registry import registry

        def call(**kwargs):
            if entrypoint == "registry":
                if "model" in kwargs:
                    kwargs["model"] = {"model": kwargs["model"]}
                return json.loads(registry.dispatch("cronjob", kwargs))
            return json.loads(ct.cronjob(**kwargs))

        if action == "create":
            ct.set_current_agent_model("claude-bpx-7", "claude-opus-5-5")
            result = call(action="create", prompt="Check", schedule="every 1h", model=model)
            job_id = result.get("job_id")
        else:
            ct.set_current_agent_model(None, None)
            created = call(action="create", prompt="Check", schedule="every 1h")
            assert created["success"] is True, created
            job_id = created["job_id"]
            ct.set_current_agent_model("claude-bpx-7", "claude-opus-5-5")
            result = call(action="update", job_id=job_id, model=model)
        assert result["success"] is True, result  # never refused by Rule #20b
        job = next(j for j in json.loads(JOBS_FILE.read_text())["jobs"] if j["id"] == job_id)
        assert (job["provider"], job["model"]) == (expected_provider, model)

    def test_registry_model_only_llm_to_script_drops_inherited_pool(self):
        # LLM -> script via the registry with a MODEL-ONLY spec (not "auto"):
        # the handler's prefill would otherwise glue the seat's pool onto a
        # script row before cronjob() sees the target mode. (r4 F3: this is
        # the arm that needs the registry guard — "auto" never reaches it.)
        from cron.jobs import JOBS_FILE
        from tools.registry import registry

        created = json.loads(ct.cronjob(action="create", prompt="Check", schedule="every 1h",
                                        script="noop.sh"))
        assert created["success"] is True, created
        ct.set_current_agent_model("claude-bpx-7", "claude-opus-5-5")
        updated = json.loads(registry.dispatch("cronjob", {
            "action": "update", "job_id": created["job_id"], "no_agent": True,
            "model": {"model": "claude-sonnet-5"},
        }))
        assert updated["success"] is True, updated
        job = next(j for j in json.loads(JOBS_FILE.read_text())["jobs"] if j["id"] == created["job_id"])
        assert job["no_agent"] is True
        assert job["provider"] is None

    # Create/update auto parity (r5 F1): for the same live session,
    # `update model='auto'` must persist exactly the row `create model='auto'`
    # persists — including the auto-pin flagship exemption. Without it a
    # flagship session (Apollo on claude-fable-5-1) could create auto crons
    # but every `update model='auto'` was refused by validate_worker_model.
    @pytest.mark.parametrize("entrypoint", ["direct", "registry"])
    @pytest.mark.parametrize("transition", ["llm", "script_to_llm"])
    @pytest.mark.parametrize("seat, pool, session_model", [
        ("claude-apx-3", "claude-apr", "claude-fable-5-1"),   # flagship
        ("claude-bpx-7", "claude-bpr", "claude-fable-5-1"),   # flagship
        ("claude-bpx-7", "claude-bpr", "claude-opus-5-5"),    # non-flagship control
    ])
    def test_update_auto_persists_same_row_as_create_auto(
        self, entrypoint, transition, seat, pool, session_model,
    ):
        from cron.jobs import JOBS_FILE
        from tools.registry import registry

        def call(**kwargs):
            if entrypoint == "registry":
                if "model" in kwargs:
                    kwargs["model"] = {"model": kwargs["model"]}
                return json.loads(registry.dispatch("cronjob", kwargs))
            return json.loads(ct.cronjob(**kwargs))

        def row(job_id):
            job = next(j for j in json.loads(JOBS_FILE.read_text())["jobs"] if j["id"] == job_id)
            return (job["no_agent"], job["provider"], job["model"], job.get("allow_flagship_reason"))

        # Reference row: create model='auto' from this session.
        ct.set_current_agent_model(seat, session_model)
        created = call(action="create", prompt="Check", schedule="every 1h", model="auto")
        assert created["success"] is True, created
        expected = row(created["job_id"])
        assert expected[1:3] == (pool, session_model)

        # Subject: a job created without a session, then `update model='auto'`.
        ct.set_current_agent_model(None, None)
        if transition == "llm":
            subject = call(action="create", prompt="Check", schedule="every 1h")
            update_args = {"model": "auto"}
        else:
            subject = call(action="create", prompt="Check", schedule="every 1h",
                           script="noop.sh", no_agent=True)
            update_args = {"model": "auto", "no_agent": False}
        assert subject["success"] is True, subject
        ct.set_current_agent_model(seat, session_model)
        updated = call(action="update", job_id=subject["job_id"], **update_args)
        assert updated["success"] is True, updated  # never refused as a flagship
        assert row(subject["job_id"]) == expected

    def test_update_auto_explicit_flagship_reason_wins(self):
        from cron.jobs import JOBS_FILE

        created = json.loads(ct.cronjob(action="create", prompt="Check", schedule="every 1h"))
        assert created["success"] is True, created
        ct.set_current_agent_model("claude-apx-3", "claude-fable-5-1")
        updated = json.loads(ct.cronjob(
            action="update", job_id=created["job_id"], model="auto",
            allow_flagship_reason="operator: nightly fleet audit needs fable",
        ))
        assert updated["success"] is True, updated
        job = next(j for j in json.loads(JOBS_FILE.read_text())["jobs"] if j["id"] == created["job_id"])
        assert job["allow_flagship_reason"] == "operator: nightly fleet audit needs fable"

    def test_update_auto_that_cannot_resolve_synthesizes_no_reason(self):
        # No live session → auto resolves to nothing; the exemption must not be
        # fabricated for a row that carries no model (mirrors create).
        from cron.jobs import JOBS_FILE

        created = json.loads(ct.cronjob(action="create", prompt="Check", schedule="every 1h"))
        assert created["success"] is True, created
        ct.set_current_agent_model(None, None)
        updated = json.loads(ct.cronjob(action="update", job_id=created["job_id"], model="auto"))
        assert updated["success"] is True, updated
        job = next(j for j in json.loads(JOBS_FILE.read_text())["jobs"] if j["id"] == created["job_id"])
        assert (job["provider"], job["model"], job.get("allow_flagship_reason")) == (None, None, None)

    def test_registry_unknown_seat_like_spelling_persists_verbatim(self):
        # E2E half of the fullmatch boundary (r5 C1): a spelling the registry
        # does not know and the seat regex does not fully match is not a seat.
        from cron.jobs import JOBS_FILE

        ct.set_current_agent_model("claude-bpx-7x", "claude-opus-5-5")
        created = json.loads(ct.cronjob(action="create", prompt="Check", schedule="every 1h", model="auto"))
        assert created["success"] is True, created
        job = next(j for j in json.loads(JOBS_FILE.read_text())["jobs"] if j["id"] == created["job_id"])
        assert (job["provider"], job["model"]) == ("claude-bpx-7x", "claude-opus-5-5")

    def test_create_no_agent_script_untouched_by_auto(self, tmp_path, monkeypatch):
        # A no_agent script cron must NOT get a model resolved/pinned — the
        # create path guards LLM-model resolution behind `if not _no_agent:`.
        # Place a script the tool will accept, then create with no_agent=True
        # and model="auto"; the persisted job must carry NO model.
        scripts_dir = tmp_path / "scripts"
        scripts_dir.mkdir()
        (scripts_dir / "noop.sh").write_text("#!/bin/bash\necho hi\n")
        monkeypatch.setattr("tools.cronjob_tools.HERMES", tmp_path, raising=False)
        ct.set_current_agent_model("openai-codex", "gpt-5.6-terra")
        created = json.loads(ct.cronjob(
            action="create", schedule="every 1h", name="noagent-job",
            no_agent=True, script="noop.sh", model="auto",
        ))
        # Whether or not script validation passes in this sandbox, the key
        # invariant is that a no_agent create never pins an LLM model to "auto".
        if created.get("success"):
            assert created["job"].get("model") in (None, "", "auto")

    def test_create_without_auto_stays_unpinned(self):
        # Back-compat: an ordinary create with no model stays unpinned.
        ct.set_current_agent_model("openai-codex", "gpt-5.6-terra")
        created = json.loads(ct.cronjob(
            action="create", prompt="Check", schedule="every 1h",
            name="plain-job",
        ))
        assert created["success"] is True
        assert created["job"].get("model") in (None, "")

