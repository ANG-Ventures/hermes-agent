"""Every non-interactive model-string entry point must resolve `model.aliases`.

`model.aliases` (e.g. `grok: xai-oauth/grok-4.6`) used to resolve ONLY in the
interactive `/model` command. Every other entry point handed the raw string to
the current provider, which 400'd, and the fallback chain silently served a
different provider AND model behind a one-line banner (measured 2026-09-18).
The CLI `-m/--model` flag was fixed first (see
tests/hermes_cli/test_startup_model_arg_alias.py); these are the remaining
sites, each of which PERSISTS or CONSUMES a user-supplied model string:

  * kanban `create --model grok`         -> create_task
  * kanban `set-model <id> grok`         -> set_model_override
  * kanban `edit --model grok`           -> set_task_model
  * cron `create --model grok`           -> create_job
  * cron `edit --model grok`             -> update_job
  * config `delegation.model: grok`      -> _resolve_delegation_credentials

For the PERSISTED sites the resolution happens at WRITE time, so retargeting
the alias later cannot silently change what an already-stored card or job runs.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import model_switch as ms


@pytest.fixture(autouse=True)
def _direct_alias_grok(monkeypatch):
    """Make `grok` a config-declared direct alias for xai-oauth/grok-4.6."""
    saved = dict(ms.DIRECT_ALIASES)
    saved_degraded = ms._DIRECT_ALIASES_DEGRADED
    ms.DIRECT_ALIASES.clear()
    ms._DIRECT_ALIASES_DEGRADED = False

    def _loader():
        merged = dict(ms._BUILTIN_DIRECT_ALIASES)
        merged["grok"] = ms.DirectAlias(
            model="grok-4.6", provider="xai-oauth", base_url=""
        )
        return merged, True

    monkeypatch.setattr(ms, "_load_direct_aliases", _loader)
    yield
    ms.DIRECT_ALIASES.clear()
    ms.DIRECT_ALIASES.update(saved)
    ms._DIRECT_ALIASES_DEGRADED = saved_degraded


# ---------------------------------------------------------------------------
# The shared write-time wrapper
# ---------------------------------------------------------------------------

def test_storage_resolver_expands_alias_to_provider_and_model():
    assert ms.resolve_model_pair_for_storage("grok", None) == (
        "grok-4.6",
        "xai-oauth",
    )


def test_storage_resolver_explicit_provider_wins():
    """`--model grok --provider claude-apr`: the explicit flag wins, like /model."""
    model, provider = ms.resolve_model_pair_for_storage("grok", "claude-apr")
    assert provider == "claude-apr"


def test_storage_resolver_passes_through_plain_ids_and_empties():
    assert ms.resolve_model_pair_for_storage("claude-opus-5", None) == (
        "claude-opus-5",
        None,
    )
    assert ms.resolve_model_pair_for_storage(None, None) == (None, None)
    assert ms.resolve_model_pair_for_storage("", "p") == ("", "p")


def test_storage_resolver_leaves_vendor_namespace_alone():
    """`vendor/model` is a model-id prefix the target provider strips, not a
    provider switch (guards tests/hermes_cli/test_codex_foreign_provider_prefix.py)."""
    assert ms.resolve_model_pair_for_storage("anthropic/claude-opus-4.6", None) == (
        "anthropic/claude-opus-4.6",
        None,
    )


# ---------------------------------------------------------------------------
# Kanban — persisted card overrides
# ---------------------------------------------------------------------------

@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    from hermes_cli import kanban_db as kb

    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def test_kanban_create_resolves_alias_at_write_time(kanban_home):
    from hermes_cli import kanban_db as kb

    with kb.connect() as conn:
        tid = kb.create_task(
            conn, title="aliased", assignee="worker", model_override="grok"
        )
    # Reload from a fresh connection — proves it PERSISTED resolved, not just
    # that a reader resolves it on the way out.
    with kb.connect() as conn:
        task = kb.get_task(conn, tid)
    assert task.model_override == "grok-4.6"
    assert task.provider_override == "xai-oauth"


def test_kanban_create_explicit_provider_still_wins(kanban_home):
    from hermes_cli import kanban_db as kb

    with kb.connect() as conn:
        tid = kb.create_task(
            conn,
            title="aliased",
            assignee="worker",
            model_override="grok",
            provider_override="claude-apr",
        )
        task = kb.get_task(conn, tid)
    assert task.provider_override == "claude-apr"


def test_kanban_set_model_override_resolves_alias(kanban_home):
    from hermes_cli import kanban_db as kb

    with kb.connect() as conn:
        tid = kb.create_task(conn, title="x", assignee="worker")
        assert kb.set_model_override(conn, tid, "grok") is True
    with kb.connect() as conn:
        task = kb.get_task(conn, tid)
    assert (task.model_override, task.provider_override) == ("grok-4.6", "xai-oauth")


def test_kanban_edit_set_task_model_resolves_alias(kanban_home):
    """`kanban edit --model grok` goes through set_task_model, a DIFFERENT
    setter from set-model — both must resolve or the class survives in one."""
    from hermes_cli import kanban_db as kb

    with kb.connect() as conn:
        tid = kb.create_task(conn, title="x", assignee="worker")
        assert kb.set_task_model(conn, tid, "grok") == 1
    with kb.connect() as conn:
        task = kb.get_task(conn, tid)
    assert (task.model_override, task.provider_override) == ("grok-4.6", "xai-oauth")


def test_kanban_stored_alias_reaches_worker_argv_resolved(kanban_home, monkeypatch):
    """End of the chain: the dispatcher's `-m` token is the resolved model and
    `--provider` names its provider — the literal word `grok` never spawns."""
    from hermes_cli import kanban_db as kb

    with kb.connect() as conn:
        tid = kb.create_task(
            conn, title="spawn", assignee="worker", model_override="grok"
        )
        task = kb.get_task(conn, tid)

    captured: dict = {}

    class FakeProc:
        pid = 4242

    def fake_popen(cmd, *a, **kw):
        captured["cmd"] = list(cmd)
        return FakeProc()

    monkeypatch.setattr("subprocess.Popen", fake_popen)
    workspace = kb.resolve_workspace(task)
    kb._default_spawn(task, str(workspace))
    argv = captured["cmd"]
    assert argv[argv.index("-m") + 1] == "grok-4.6"
    assert argv[argv.index("--provider") + 1] == "xai-oauth"
    assert "grok" not in argv


def test_kanban_non_alias_model_is_still_stored_verbatim(kanban_home):
    """Unresolvable strings keep the previous literal-storage behaviour."""
    from hermes_cli import kanban_db as kb

    with kb.connect() as conn:
        tid = kb.create_task(conn, title="x", assignee="worker")
        assert kb.set_task_model(conn, tid, "x ; y") == 1
        assert kb.get_task(conn, tid).model_override == "x ; y"


# ---------------------------------------------------------------------------
# Cron — persisted job pins
# ---------------------------------------------------------------------------

@pytest.fixture
def cron_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    return home


def test_cron_create_job_resolves_alias_at_write_time(cron_home):
    from cron import jobs as cj

    job = cj.create_job(prompt="ping", schedule="in 1 hour", model="grok")
    assert job["model"] == "grok-4.6"
    assert job["provider"] == "xai-oauth"
    # Reload from disk — proves the resolved pair PERSISTED.
    reloaded = [j for j in cj.load_jobs() if j["id"] == job["id"]][0]
    assert (reloaded["model"], reloaded["provider"]) == ("grok-4.6", "xai-oauth")


def test_cron_create_job_explicit_provider_wins(cron_home):
    from cron import jobs as cj

    job = cj.create_job(
        prompt="ping", schedule="in 1 hour", model="grok", provider="claude-apr"
    )
    assert job["provider"] == "claude-apr"


def test_cron_update_job_resolves_alias(cron_home):
    from cron import jobs as cj

    job = cj.create_job(prompt="ping", schedule="in 1 hour")
    updated = cj.update_job(job["id"], {"model": "grok"})
    assert (updated["model"], updated["provider"]) == ("grok-4.6", "xai-oauth")


def test_cron_update_job_empty_model_still_clears_the_pin(cron_home):
    """The clear semantic ('' clears the pin) must survive the resolution hook."""
    from cron import jobs as cj

    job = cj.create_job(prompt="ping", schedule="in 1 hour", model="grok")
    assert job["model"] == "grok-4.6"
    updated = cj.update_job(job["id"], {"model": ""})
    assert not updated["model"]


def test_cron_create_job_unpinned_model_is_untouched(cron_home):
    from cron import jobs as cj

    job = cj.create_job(prompt="ping", schedule="in 1 hour")
    assert job["model"] is None


# ---------------------------------------------------------------------------
# delegate_task — config `delegation.model`
# ---------------------------------------------------------------------------

def test_delegation_config_model_alias_resolves(monkeypatch):
    """`delegation.model: grok` must not reach the child's provider raw.

    Resolved at READ time (the config file is the user's, not our record).
    The resolved pair is what the runtime provider resolution is asked for.
    """
    from tools import delegate_tool as dt
    import hermes_cli.runtime_provider as rp

    seen: dict = {}

    def fake_resolve(requested=None, target_model=None, **kw):
        seen["provider"] = requested
        seen["model"] = target_model
        return {
            "provider": requested,
            "model": target_model,
            "base_url": "https://example.invalid",
            "api_key": "k",
            "api_mode": "chat_completions",
        }

    monkeypatch.setattr(rp, "resolve_runtime_provider", fake_resolve)
    creds = dt._resolve_delegation_credentials({"model": "grok"}, parent_agent=None)
    assert seen == {"provider": "xai-oauth", "model": "grok-4.6"}
    assert creds["model"] == "grok-4.6"
    assert creds["provider"] == "xai-oauth"


def test_delegation_config_unresolvable_alias_provider_fails_loud():
    """An alias resolving to a provider with no credentials raises a message
    NAMING that provider — instead of silently inheriting the parent's
    provider and letting the fallback chain serve a different model."""
    from tools import delegate_tool as dt

    with pytest.raises(ValueError) as exc:
        dt._resolve_delegation_credentials({"model": "grok"}, parent_agent=None)
    assert "xai-oauth" in str(exc.value)


def test_delegation_config_plain_model_is_untouched():
    from tools import delegate_tool as dt

    creds = dt._resolve_delegation_credentials(
        {"model": "claude-opus-5"}, parent_agent=None
    )
    assert creds["model"] == "claude-opus-5"
