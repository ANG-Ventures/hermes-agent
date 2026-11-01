"""Dispatcher-to-CLI seam coverage for Kanban worker chat identity.

Kanban workers are spawned as ``hermes ... chat -q``, which constructs the
agent through ``CLIAgentSetupMixin._init_agent``.  ``hermes_cli.oneshot`` is the
separate ``-z`` path, so testing or fixing only that module leaves dispatched
worker turns anonymous.
"""

from __future__ import annotations

import subprocess
from types import SimpleNamespace

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli.cli_agent_setup_mixin import CLIAgentSetupMixin


def _task() -> kb.Task:
    return kb.Task(
        id="t_identity",
        title="worker identity",
        body=None,
        assignee="worker",
        status="running",
        priority=0,
        created_by="test",
        created_at=1,
        started_at=None,
        completed_at=None,
        workspace_kind="dir",
        workspace_path=None,
        claim_lock="lock",
        claim_expires=None,
        tenant=None,
        current_run_id=7,
    )


def _capture_spawn_env(monkeypatch, tmp_path) -> dict[str, str]:
    root = tmp_path / ".hermes"
    profile = root / "profiles" / "worker"
    profile.mkdir(parents=True)
    profile.joinpath("config.yaml").write_text("{}\n", encoding="utf-8")
    root.joinpath("config.yaml").write_text("{}\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(root))
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.setattr(kbd, "_resolve_hermes_argv", lambda: ["hermes"])
    monkeypatch.setattr(kbd, "_resolve_worker_cli_toolsets", lambda _home: None)
    monkeypatch.setattr(kb, "_kanban_worker_skill_available", lambda _home: False)

    captured: dict[str, object] = {}

    class FakeProc:
        pid = 4242

    def fake_popen(cmd, *args, **kwargs):
        captured["env"] = dict(kwargs["env"])
        return FakeProc()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    kb.create_board("identity-board")

    assert kbd._default_spawn(_task(), str(workspace), board="identity-board") == 4242
    return captured["env"]  # type: ignore[return-value]


class _FakeCLI(CLIAgentSetupMixin):
    pass


def _fake_cli() -> _FakeCLI:
    noop = lambda *_args, **_kwargs: None
    cli = _FakeCLI()
    cli.__dict__.update(vars(SimpleNamespace(
        agent=None,
        _install_tool_callbacks=noop,
        _ensure_tirith_security=noop,
        finalize_preloaded_skills=noop,
        _ensure_runtime_credentials=lambda: True,
        _current_reasoning_callback=lambda: None,
        _session_db=object(),
        _resumed=False,
        conversation_history=[],
        api_key="test-key",
        base_url="https://example.invalid/v1",
        provider="test",
        requested_provider="test",
        api_mode="chat_completions",
        acp_command=None,
        acp_args=[],
        _credential_pool=None,
        model="test-model",
        max_tokens=None,
        max_turns=3,
        enabled_toolsets=[],
        disabled_toolsets=[],
        verbose=False,
        tool_progress_mode="off",
        system_prompt="",
        prefill_messages=[],
        reasoning_config=None,
        service_tier=None,
        _providers_only=None,
        _providers_ignore=None,
        _providers_order=None,
        _provider_sort=None,
        _provider_require_params=False,
        _provider_data_collection=None,
        _openrouter_min_coding_score=None,
        session_id="session-test",
        _clarify_callback=noop,
        _connection_callback=noop,
        _agent_status_print=noop,
        _active_agent_route_signature=None,
        _fallback_model=[],
        _on_thinking=noop,
        checkpoints_enabled=False,
        checkpoint_max_snapshots=20,
        checkpoint_max_total_size_mb=500,
        checkpoint_max_file_size_mb=10,
        pass_session_id=False,
        ignore_rules=False,
        _on_tool_progress=noop,
        _inline_diffs_enabled=False,
        _on_tool_start=noop,
        _on_tool_complete=noop,
        streaming_enabled=False,
        _stream_delta=noop,
        _on_tool_gen_start=noop,
        _on_notice=noop,
        _on_notice_clear=noop,
        _on_reaction=noop,
        _pending_title=False,
    )))
    return cli


def test_dispatcher_env_reaches_chat_q_agent_identity(monkeypatch, tmp_path):
    """The real dispatcher env must identify the agent built by ``chat -q``."""
    env = _capture_spawn_env(monkeypatch, tmp_path)
    assert env["HERMES_KANBAN_TASK"] == "t_identity"
    assert "HERMES_KANBAN_BOARD" not in env  # S7: the board is derived from the DB pin
    monkeypatch.setenv("HERMES_KANBAN_TASK", env["HERMES_KANBAN_TASK"])
    monkeypatch.setenv("HERMES_KANBAN_DB", env["HERMES_KANBAN_DB"])

    import cli as cli_mod
    from agent import credits_tracker
    from hermes_cli import mcp_startup

    captured: dict[str, object] = {}

    def fake_agent(*args, **kwargs):
        captured.update(kwargs)
        return SimpleNamespace()

    # ``_init_agent`` (hermes_cli/cli_agent_setup_mixin.py) imports ``AIAgent`` from
    # ``run_agent`` at call time; ``cli`` no longer re-exports it.
    import run_agent as run_agent_mod
    monkeypatch.setattr(run_agent_mod, "AIAgent", fake_agent)
    monkeypatch.setattr(cli_mod, "_active_agent_ref", None)
    monkeypatch.setattr(cli_mod, "_prepare_deferred_agent_startup", lambda: None)
    monkeypatch.setattr(mcp_startup, "wait_for_mcp_discovery", lambda: None)
    monkeypatch.setattr(credits_tracker, "seed_credits_at_session_start", lambda _agent: None)

    assert _fake_cli()._init_agent() is True
    assert captured["chat_id"] == "t_identity"
    assert captured["chat_name"] == "kanban / identity-board / t_identity"


def test_foreign_root_env_names_its_own_board(monkeypatch, tmp_path):
    """Prism f984ec16d458: an ``env`` describing a worker under ANOTHER Hermes root resolves its
    pin against THAT root's board registry, never the process root's."""
    from hermes_cli.kanban_identity import resolve_kanban_worker_chat_identity

    a_root = tmp_path / "a" / ".hermes"
    b_root = tmp_path / "b" / ".hermes"
    for root in (a_root, b_root):
        root.mkdir(parents=True)
        root.joinpath("config.yaml").write_text("{}\n", encoding="utf-8")
    monkeypatch.delenv("HERMES_KANBAN_HOME", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.setenv("HERMES_HOME", str(b_root))
    kb.create_board("beta")  # board beta exists ONLY under root b
    beta_db = b_root / "kanban" / "boards" / "beta" / "kanban.db"
    assert beta_db.parent.is_dir()
    monkeypatch.setenv("HERMES_HOME", str(a_root))  # the process root is a

    env = {
        "HOME": str(tmp_path / "b"),
        "HERMES_HOME": str(b_root),
        "HERMES_KANBAN_TASK": "t_12345678",
        "HERMES_KANBAN_DB": str(beta_db),
    }
    chat_id, chat_name = resolve_kanban_worker_chat_identity(env=env)
    assert chat_id == "t_12345678"
    assert chat_name == "kanban / beta / t_12345678"


def test_foreign_env_tilde_kanban_home_expands_against_env_home(monkeypatch, tmp_path):
    """Prism 9342cb794d7b: a described env's ``HERMES_KANBAN_HOME=~/.hermes`` expands against
    THAT env's ``HOME``, never the process HOME, so its pin resolves in its own registry."""
    from hermes_cli.kanban_identity import resolve_kanban_worker_chat_identity

    operator_home = tmp_path / "operator"
    worker_home = tmp_path / "worker"
    worker_root = worker_home / ".hermes"
    operator_root = operator_home / ".hermes"
    for root in (operator_root, worker_root):
        root.mkdir(parents=True)
        root.joinpath("config.yaml").write_text("{}\n", encoding="utf-8")
    monkeypatch.delenv("HERMES_KANBAN_HOME", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.setenv("HOME", str(worker_home))
    monkeypatch.setenv("HERMES_HOME", str(worker_root))
    kb.create_board("alpha")  # board alpha exists ONLY under the worker's root
    alpha_db = worker_root / "kanban" / "boards" / "alpha" / "kanban.db"
    assert alpha_db.parent.is_dir()
    monkeypatch.setenv("HOME", str(operator_home))  # the process HOME is the operator's
    monkeypatch.setenv("HERMES_HOME", str(operator_root))

    env = {
        "HOME": str(worker_home),
        "HERMES_KANBAN_HOME": "~/.hermes",
        "HERMES_KANBAN_DB": str(alpha_db),
        "HERMES_KANBAN_TASK": "t_example",
    }
    assert resolve_kanban_worker_chat_identity(env=env) == ("t_example", "kanban / alpha / t_example")


def test_foreign_env_tilde_hermes_home_expands_against_env_home(monkeypatch, tmp_path):
    """Prism ba00e8f88abd: a described env's ``HERMES_HOME=~/.hermes`` (no KANBAN_HOME) expands
    against THAT env's ``HOME`` too: every env-described path goes through ``_env_path``."""
    from hermes_cli.kanban_identity import resolve_kanban_worker_chat_identity

    operator_home = tmp_path / "operator"
    worker_home = tmp_path / "worker"
    worker_root = worker_home / ".hermes"
    operator_root = operator_home / ".hermes"
    for root in (operator_root, worker_root):
        root.mkdir(parents=True)
        root.joinpath("config.yaml").write_text("{}\n", encoding="utf-8")
    monkeypatch.delenv("HERMES_KANBAN_HOME", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.setenv("HOME", str(worker_home))
    monkeypatch.setenv("HERMES_HOME", str(worker_root))
    kb.create_board("alpha")  # board alpha exists ONLY under the worker's root
    alpha_db = worker_root / "kanban" / "boards" / "alpha" / "kanban.db"
    assert alpha_db.parent.is_dir()
    monkeypatch.setenv("HOME", str(operator_home))  # the process HOME is the operator's
    monkeypatch.setenv("HERMES_HOME", str(operator_root))

    env = {
        "HOME": str(worker_home),
        "HERMES_HOME": "~/.hermes",
        "HERMES_KANBAN_DB": str(alpha_db),
        "HERMES_KANBAN_TASK": "t_example",
    }
    assert resolve_kanban_worker_chat_identity(env=env) == ("t_example", "kanban / alpha / t_example")


def test_foreign_env_home_alone_names_its_own_root(monkeypatch, tmp_path):
    """Prism 03487e108873: an env whose ``HOME`` differs from this process's, naming neither
    ``HERMES_KANBAN_HOME`` nor ``HERMES_HOME``, describes ``<env HOME>/.hermes``, never this
    process's root."""
    from hermes_cli.kanban_identity import resolve_kanban_worker_chat_identity

    operator_home = tmp_path / "operator"
    worker_home = tmp_path / "worker"
    worker_root = worker_home / ".hermes"
    operator_root = operator_home / ".hermes"
    for root in (operator_root, worker_root):
        root.mkdir(parents=True)
        root.joinpath("config.yaml").write_text("{}\n", encoding="utf-8")
    monkeypatch.delenv("HERMES_KANBAN_HOME", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.setenv("HOME", str(worker_home))
    monkeypatch.setenv("HERMES_HOME", str(worker_root))
    kb.create_board("alpha")  # board alpha exists ONLY under the worker's root
    alpha_db = worker_root / "kanban" / "boards" / "alpha" / "kanban.db"
    assert alpha_db.parent.is_dir()
    monkeypatch.setenv("HOME", str(operator_home))
    monkeypatch.setenv("HERMES_HOME", str(operator_root))

    env = {"HOME": str(worker_home), "HERMES_KANBAN_DB": str(alpha_db),
           "HERMES_KANBAN_TASK": "t_example"}
    assert resolve_kanban_worker_chat_identity(env=env) == ("t_example", "kanban / alpha / t_example")
    # An env whose HOME IS this process's still means this process's root.
    from hermes_cli.kanban_identity import _env_kanban_root

    assert _env_kanban_root({"HOME": str(operator_home)}) is None
    assert _env_kanban_root({}) is None


def test_kanban_identity_expands_user_paths_only_in_env_path():
    """Prism ba00e8f88abd as a RULE (operator-requested source contract): the only ``expanduser``
    in kanban_identity is ``_env_path``'s no-HOME fallback, so a new env-described path cannot
    expand ``~`` against the process."""
    import ast
    import inspect

    from hermes_cli import kanban_identity

    tree = ast.parse(inspect.getsource(kanban_identity))
    owners = [fn.name for fn in ast.walk(tree)
              if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef))
              for node in ast.walk(fn)
              if isinstance(node, ast.Attribute) and node.attr == "expanduser"]
    assert owners and set(owners) == {"_env_path"}, owners


def test_env_suffix_on_our_home_names_the_suffixed_root(monkeypatch, tmp_path):
    """Prism 976039e81f74: an env sharing this process's ``HOME`` but carrying a different
    ``HERMES_DATA_DIR_SUFFIX`` describes ``<HOME>/.hermes<suffix>``, never the process root."""
    from hermes_cli.kanban_identity import _env_kanban_root, resolve_kanban_worker_chat_identity

    home = tmp_path / "alice"
    ci_root = home / ".hermes-ci"
    ci_root.mkdir(parents=True)
    ci_root.joinpath("config.yaml").write_text("{}\n", encoding="utf-8")
    monkeypatch.delenv("HERMES_KANBAN_HOME", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.delenv("HERMES_DATA_DIR_SUFFIX", raising=False)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("HERMES_HOME", str(ci_root))
    kb.create_board("beta")  # board beta exists ONLY under the suffixed root
    beta_db = ci_root / "kanban" / "boards" / "beta" / "kanban.db"
    assert beta_db.parent.is_dir()
    monkeypatch.delenv("HERMES_HOME")  # the process root is now <home>/.hermes

    env = {"HOME": str(home), "HERMES_DATA_DIR_SUFFIX": "-ci",
           "HERMES_KANBAN_DB": str(beta_db), "HERMES_KANBAN_TASK": "t_example"}
    assert _env_kanban_root(env) == home / ".hermes-ci"
    assert resolve_kanban_worker_chat_identity(env=env) == ("t_example", "kanban / beta / t_example")
    # Same HOME and same (empty) suffix still means this process's root.
    assert _env_kanban_root({"HOME": str(home)}) is None
    assert _env_kanban_root({"HOME": str(home), "HERMES_DATA_DIR_SUFFIX": ""}) is None


def test_env_without_root_vars_inherits_the_process_suffix(monkeypatch, tmp_path):
    """Prism 976039e81f74 (round 2): an absent key inherits the process value, never ``""``. With a
    process suffix, a mapping naming no root-selection variable is the process root (``None``); a
    mapping that sets the suffix explicitly still names its own root."""
    from hermes_cli.kanban_identity import _env_kanban_root

    home = tmp_path / "alice"
    home.mkdir()
    for var in ("HERMES_KANBAN_HOME", "HERMES_HOME"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("HERMES_DATA_DIR_SUFFIX", "-ci")
    assert _env_kanban_root({}) is None
    assert _env_kanban_root({"HOME": str(home)}) is None
    assert _env_kanban_root({"HOME": str(home), "HERMES_DATA_DIR_SUFFIX": ""}) == home / ".hermes"
    assert _env_kanban_root({"HERMES_DATA_DIR_SUFFIX": "-qa"}) == home / ".hermes-qa"


def test_env_on_our_home_is_not_our_root_when_hermes_home_moved_it(monkeypatch, tmp_path):
    """Prism 2be710857d51: ``None`` only when the root the env describes EQUALS the process root.
    A process rooted by ``HERMES_HOME=/x`` and an env with our ``HOME`` but no ``HERMES_HOME``
    describe different roots, so the env's default root is returned and its pin names its board."""
    from hermes_cli.kanban_identity import _env_kanban_root, resolve_kanban_worker_chat_identity

    home = tmp_path / "alice"
    default_root = home / ".hermes"
    custom_root = tmp_path / "x"
    for root in (default_root, custom_root):
        root.mkdir(parents=True)
        root.joinpath("config.yaml").write_text("{}\n", encoding="utf-8")
    for var in ("HERMES_KANBAN_HOME", "HERMES_KANBAN_DB", "HERMES_DATA_DIR_SUFFIX"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("HERMES_HOME", str(default_root))
    kb.create_board("alpha")  # board alpha exists ONLY under the default root
    alpha_db = default_root / "kanban" / "boards" / "alpha" / "kanban.db"
    assert alpha_db.parent.is_dir()
    monkeypatch.setenv("HERMES_HOME", str(custom_root))  # the process root is now /x

    env = {"HOME": str(home), "HERMES_KANBAN_DB": str(alpha_db), "HERMES_KANBAN_TASK": "t_example"}
    assert _env_kanban_root(env) == default_root
    assert resolve_kanban_worker_chat_identity(env=env) == ("t_example", "kanban / alpha / t_example")
    # An env describing exactly the process root is the process root.
    assert _env_kanban_root({"HERMES_HOME": str(custom_root)}) is None
    assert _env_kanban_root({"HOME": str(home), "HERMES_HOME": str(custom_root)}) is None
    assert _env_kanban_root({"HERMES_KANBAN_HOME": str(custom_root)}) is None


def _our_home(monkeypatch, tmp_path):
    home = tmp_path / "alice"
    (home / ".hermes").mkdir(parents=True)
    for var in ("HERMES_KANBAN_HOME", "HERMES_KANBAN_DB", "HERMES_DATA_DIR_SUFFIX", "HERMES_KANBAN_SANDBOX"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("HERMES_HOME", str(home / ".hermes"))
    return home


def test_env_on_our_home_is_our_root_under_a_process_kanban_override(monkeypatch, tmp_path):
    """Prism 0c5ff7fb032a: an env naming only our HOME is compared with the process's DEFAULT root,
    never ``kanban_home()``. Under a process ``HERMES_KANBAN_HOME`` it means the
    process root (``None``), not the live ``<HOME>/.hermes`` registry."""
    from hermes_cli.kanban_identity import _env_kanban_root

    home = _our_home(monkeypatch, tmp_path)
    override = tmp_path / "x"
    override.mkdir()
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(override))
    assert _env_kanban_root({"HOME": str(home)}) is None
    assert _env_kanban_root({"HERMES_KANBAN_HOME": str(override)}) is None


def test_env_on_our_home_is_our_root_under_the_sandbox(monkeypatch, tmp_path):
    """Prism 0c5ff7fb032a, sandbox arm: ``kanban_home()`` is the sandbox dir, the env's HOME
    describes the same default root as the process, so ``None``, never the live registry."""
    from hermes_cli.kanban_identity import _env_kanban_root

    home = _our_home(monkeypatch, tmp_path)
    scratch = home / ".hermes" / "profiles" / "p" / "cache" / "scratch" / "x"
    scratch.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(scratch))
    monkeypatch.setenv("HERMES_KANBAN_SANDBOX", "1")
    monkeypatch.setattr(kb, "_live_kanban_roots", lambda: (home / ".hermes",))
    assert kb.kanban_home() == scratch  # the sandbox root differs from the default root
    assert _env_kanban_root({"HOME": str(home)}) is None


def test_env_root_resolve_error_is_the_process_root(monkeypatch, tmp_path):
    """A refused/unresolvable process root answers ``None`` (the guarded process root), never the
    env root, which can be the live one (Prism 0c5ff7fb032a)."""
    import hermes_constants
    from hermes_cli.kanban_identity import _env_kanban_root

    home = _our_home(monkeypatch, tmp_path)

    def refuse():
        raise kb.KanbanLiveBoardRefusedError("refused")

    real_default = hermes_constants.get_default_hermes_root

    def default_root(*, home=None):
        if home is None:
            raise kb.KanbanLiveBoardRefusedError("unresolvable")
        return real_default(home=home)

    monkeypatch.setattr(kb, "kanban_home", refuse)
    monkeypatch.setattr(hermes_constants, "get_default_hermes_root", default_root)
    assert _env_kanban_root({"HOME": str(home)}) is None
