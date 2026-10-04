"""Profile-bound agent construction must arm real shell policy."""
import os
import shlex
import subprocess
import sys
from pathlib import Path

import hermes_yaml as yaml


def test_agent_build_arms_only_consented_profile_policy(tmp_path, monkeypatch):
    from agent import shell_hooks
    from hermes_cli import plugins
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override
    from tui_gateway import server

    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setenv('HOME', str(tmp_path / 'os-home'))
    monkeypatch.setattr(Path, 'home', lambda: tmp_path / 'os-home')
    monkeypatch.setenv('HERMES_IGNORE_RULES', '1')
    monkeypatch.delenv('HERMES_ACCEPT_HOOKS', raising=False)
    monkeypatch.setattr(server, '_hermes_home', tmp_path)
    monkeypatch.setattr(server, '_get_db', lambda: None)
    # Only provider resolution is a fixture; registration, agent, hook process,
    # consent, manager selection and public dispatch are the real implementation.
    monkeypatch.setattr(server, '_resolve_agent_model_runtime', lambda *_a: (
        'fixture-model', {'provider': 'openai-compat', 'base_url': 'http://127.0.0.1:18019/v1', 'api_key': 'fixture-key'}))
    plugins._reset_plugin_managers_for_tests()
    shell_hooks.reset_for_tests()
    try:
        for label, consent in [('alpha', True), ('beta', True), ('unapproved', False)]:
            home = tmp_path / label
            home.mkdir()
            script = home / 'guard.py'
            script.write_text('import json\nprint(json.dumps({"decision":"block","reason":' + repr(label) + '}))\n', encoding='utf-8')
            quote_command = subprocess.list2cmdline if os.name == 'nt' else shlex.join
            cfg = {'hooks_auto_accept': consent, 'hooks': {'pre_tool_call': [{
                'command': quote_command([sys.executable, str(script)]), 'matcher': 'write_file', 'fail_closed': True}]},
                'toolsets': ['file'], 'memory': {'memory_enabled': False, 'user_profile_enabled': False}}
            (home / 'config.yaml').write_text(yaml.safe_dump(cfg), encoding='utf-8')
        for label in ['alpha', 'beta', 'alpha', 'unapproved']:
            token = set_hermes_home_override(tmp_path / label)
            try:
                agent = server._make_agent(label, label, context_cwd_is_launch_artifact=False)
                assert agent is not None
                block = plugins.get_pre_tool_call_block_message('write_file', {'path': str(tmp_path / 'protected'), 'content': 'x'})
                assert block == (None if label == 'unapproved' else label)
                assert plugins.get_pre_tool_call_block_message('read_file', {'path': str(tmp_path / 'protected')}) is None
            finally:
                reset_hermes_home_override(token)
    finally:
        plugins._reset_plugin_managers_for_tests()
        shell_hooks.reset_for_tests()

def test_identical_hook_registers_once_per_home(tmp_path, monkeypatch):
    """Two profiles with the SAME hook command each get it on their own manager."""
    from agent import shell_hooks
    from hermes_cli import plugins
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    plugins._reset_plugin_managers_for_tests()
    shell_hooks.reset_for_tests()
    cfg = {"hooks_auto_accept": True,
           "hooks": {"pre_tool_call": [{"command": "/bin/true", "matcher": "x"}]}}
    try:
        for label in ["a", "b", "a"]:
            (tmp_path / label).mkdir(exist_ok=True)
            token = set_hermes_home_override(str(tmp_path / label))
            try:
                shell_hooks.register_from_config(cfg)
                assert len(plugins.get_plugin_manager()._hooks.get("pre_tool_call", [])) == 1
            finally:
                reset_hermes_home_override(token)
    finally:
        plugins._reset_plugin_managers_for_tests()
        shell_hooks.reset_for_tests()


def test_hook_subprocess_sees_the_session_profile_home(tmp_path, monkeypatch):
    """The override is a ContextVar; the hook child must get it as its env home."""
    from agent import shell_hooks
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override

    launch = tmp_path / "launch"
    session_home = tmp_path / "session"
    launch.mkdir()
    session_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(launch))
    script = tmp_path / "echo_home.py"
    script.write_text(
        "import json, os\nprint(json.dumps({'decision': 'block', 'reason': os.environ['HERMES_HOME']}))\n",
        encoding="utf-8",
    )
    spec = shell_hooks.ShellHookSpec(
        event="pre_tool_call", command=shlex.join([sys.executable, str(script)]))
    assert '"reason": "%s"' % launch in shell_hooks._spawn_once(spec, "{}")["stdout"]
    token = set_hermes_home_override(str(session_home))
    try:
        assert '"reason": "%s"' % session_home in shell_hooks._spawn_once(spec, "{}")["stdout"]
    finally:
        reset_hermes_home_override(token)
