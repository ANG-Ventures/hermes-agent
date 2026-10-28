"""The terminal session reports its shell's GIT_* repository variables (card t_20695198).

A pre_tool_call policy hook (hermes-home hooks/live_tree_branch_policy.py) cannot see the session
shell's env: ``export GIT_DIR=<live>/.git`` in one call arms every later ``git checkout -- x`` in the
session, whatever directory it runs from. Inferring that from command text never converged (any
spelling of the name is a new bypass). The cwd marker line already reports the shell's own state
after each command; it now also reports which of the five GIT_* names are SET, the terminal tool
records that per session beside cwd, and shell_hooks ships it as ``session_git_env``.

Mirrors the cwd tests (test_interrupted_command_cwd.py, test_shell_hooks.py): a killed command emits
no marker and keeps the previous record; the model-visible output never carries the report.
"""

import json

import pytest

import tools.terminal_tool as tt
from tools.environments.base_session_env import (
    SESSION_GIT_ENV_NAMES, _cwd_marker, _cwd_marker_printf, _split_cwd_marker, _split_git_env)
from tools.environments.local import LocalEnvironment

_NONE_SET = {name: False for name in SESSION_GIT_ENV_NAMES}
_SCRUB = "unset " + " ".join(SESSION_GIT_ENV_NAMES)


@pytest.fixture(autouse=True)
def _clean_store(monkeypatch):
    monkeypatch.setattr(tt, "_session_cwd", {})
    monkeypatch.setattr(tt, "_shell_git_env", {})
    monkeypatch.setattr(tt, "_session_shell", {})
    monkeypatch.setattr(tt, "_task_env_overrides", {})
    monkeypatch.delenv("TERMINAL_ENV", raising=False)
    for name in SESSION_GIT_ENV_NAMES:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def env(tmp_path):
    environment = LocalEnvironment(cwd=str(tmp_path), timeout=30)
    environment.init_session()
    yield environment
    environment.cleanup()


class TestMarkerParse:
    M = _cwd_marker("abc123")

    def test_report_parses_and_line_is_stripped(self):
        out = f"hello\n\n{self.M}/tmp/x{self.M}git: GIT_DIR=1 GIT_INDEX_FILE=1\n"
        assert _split_git_env(out, self.M) == {**_NONE_SET, "GIT_DIR": True, "GIT_INDEX_FILE": True}
        cwd, cleaned = _split_cwd_marker(out, self.M)
        assert cwd == "/tmp/x"                       # the git section never leaks into the cwd
        assert cleaned == "hello\n"

    def test_none_set(self):
        assert _split_git_env(f"\n{self.M}/tmp{self.M}git:\n", self.M) == _NONE_SET

    def test_no_marker_or_no_section_is_none(self):
        assert _split_git_env("killed before the wrapper finished", self.M) is None
        assert _split_git_env(f"{self.M}/tmp", self.M) is None                     # half a pair
        assert _split_git_env(f"\n{self.M}/tmp{self.M}\n", self.M) is None         # old wrapper shape

    def test_unknown_words_are_ignored(self):
        out = f"\n{self.M}/tmp{self.M}git: GIT_DIR=0 FOO=1 GIT_WORK_TREE=1\n"
        assert _split_git_env(out, self.M) == {**_NONE_SET, "GIT_WORK_TREE": True}

    def test_printf_reads_set_not_value(self):
        snippet = _cwd_marker_printf(self.M)
        for name in SESSION_GIT_ENV_NAMES:
            assert f"${{{name}+ {name}=1}}" in snippet    # ${NAME+...}: set (even empty), never the value


class TestEnvironmentReportsTheShell:
    def test_export_is_reported_and_output_is_untouched(self, env, tmp_path):
        result = env.execute(f"export GIT_DIR={tmp_path}/.git; echo hi", cwd=str(tmp_path))
        assert result["output"].strip() == "hi"
        assert "git:" not in result["output"] and "GIT_DIR" not in result["output"]
        assert result["git_env"] == {**_NONE_SET, "GIT_DIR": True}

    def test_export_persists_to_the_next_command_and_unset_clears(self, env, tmp_path):
        env.execute(f"export GIT_WORK_TREE={tmp_path}", cwd=str(tmp_path))
        assert env.execute("true", cwd=str(tmp_path))["git_env"]["GIT_WORK_TREE"] is True
        assert env.execute("unset GIT_WORK_TREE", cwd=str(tmp_path))["git_env"] == _NONE_SET
        assert env.execute("true", cwd=str(tmp_path))["git_env"] == _NONE_SET

    @pytest.mark.parametrize("command", [
        "n=GIT; export ${n}_DIR=/x",                           # variable-built name
        "export GIT_DIR=/x; readonly GIT_DIR; unset GIT_DIR",  # readonly + failed unset
        ": ${GIT_DIR:=/x}; export GIT_DIR",                    # default-assign expansion
        "export GIT_DIR=/x; env -u GIT_DIR true",              # env -u only scopes its child
        "export GIT_DIR=/x; unset\u00a0GIT_DIR",                 # U+00A0 is not a separator
        "declare -x GIT_COMMON_DIR=/x",
        "read -r GIT_OBJECT_DIRECTORY <<< /x; export GIT_OBJECT_DIRECTORY",
    ])
    def test_spellings_the_text_matcher_chased_are_all_just_set(self, env, tmp_path, command):
        result = env.execute(command, cwd=str(tmp_path))
        assert any(result["git_env"].values()), result

    def test_empty_value_counts_as_set(self, env, tmp_path):
        assert env.execute("export GIT_INDEX_FILE=", cwd=str(tmp_path))["git_env"]["GIT_INDEX_FILE"] is True

    def test_inherited_process_env_is_reported(self, monkeypatch, tmp_path):
        monkeypatch.setenv("GIT_DIR", str(tmp_path / ".git"))
        environment = LocalEnvironment(cwd=str(tmp_path), timeout=30)
        environment.init_session()
        try:
            assert environment.execute("true", cwd=str(tmp_path))["git_env"]["GIT_DIR"] is True
        finally:
            environment.cleanup()

    @pytest.mark.platforms("linux")
    def test_killed_command_reports_nothing(self, env, tmp_path):
        result = env.execute("export GIT_DIR=/x; sleep 20", cwd=str(tmp_path), timeout=2)
        assert "git_env" not in result


class TestTerminalToolRecordsIt:
    """Drive ``tt.terminal_tool`` itself, like TestTerminalToolReadsTheFlag for cwd."""

    def _tool(self, monkeypatch, env, command, task_id, timeout=None, workdir=None):
        monkeypatch.setattr(tt, "_active_environments", {"default": env})
        monkeypatch.setattr(tt, "_last_activity", {})
        monkeypatch.setattr(
            tt, "_get_env_config",
            lambda: {"env_type": "local", "cwd": env.cwd, "timeout": 60, "lifetime_seconds": 3600},
        )
        monkeypatch.setattr(tt, "_check_all_guards", lambda command, env_type, **kwargs: {"approved": True})
        return json.loads(tt.terminal_tool(command=command, task_id=task_id, timeout=timeout, workdir=workdir))

    def test_record_follows_the_shell(self, env, monkeypatch, tmp_path):
        assert tt.get_session_git_env("mine") is None
        result = self._tool(monkeypatch, env, "export GIT_DIR=/x/.git; echo ok", "mine")
        assert result["output"] == "ok"
        assert tt.get_session_git_env("mine") == {**_NONE_SET, "GIT_DIR": True}
        self._tool(monkeypatch, env, _SCRUB, "mine")
        assert tt.get_session_git_env("mine") == _NONE_SET

    def test_workdir_command_still_records(self, env, monkeypatch, tmp_path):
        self._tool(monkeypatch, env, "export GIT_DIR=/x/.git", "mine", workdir=str(tmp_path))
        assert tt.get_session_git_env("mine")["GIT_DIR"] is True

    @pytest.mark.platforms("linux")
    def test_interrupt_keeps_the_previous_record(self, env, monkeypatch, tmp_path):
        self._tool(monkeypatch, env, "export GIT_DIR=/x/.git", "mine")
        self._tool(monkeypatch, env, "unset GIT_DIR; sleep 20", "mine", timeout=2)
        assert tt.get_session_git_env("mine")["GIT_DIR"] is True

    def test_export_by_one_session_reaches_every_session_on_the_shell(self, env, monkeypatch):
        """Prism 8cf2a5f81e5c / 7fc5cadf95a1 / 985402e3796a: the snapshot is shared by every session on the
        environment, so B's record must follow A's export, and A's unset must clear it for B too."""
        self._tool(monkeypatch, env, "true", "b")
        assert tt.get_session_git_env("b") == _NONE_SET
        self._tool(monkeypatch, env, "export GIT_DIR=/live/.git GIT_WORK_TREE=/live", "a")
        assert tt.get_session_git_env("b") == {**_NONE_SET, "GIT_DIR": True, "GIT_WORK_TREE": True}
        # ... and the shell really does carry it into B's next command (the premise of the finding).
        assert self._tool(monkeypatch, env, 'printf %s "$GIT_DIR"', "b")["output"] == "/live/.git"
        self._tool(monkeypatch, env, _SCRUB, "a")
        assert tt.get_session_git_env("b") == _NONE_SET

    def test_a_different_shell_does_not_leak(self, env, monkeypatch, tmp_path):
        other = LocalEnvironment(cwd=str(tmp_path), timeout=30)
        other.init_session()
        try:
            self._tool(monkeypatch, other, "true", "b")
            self._tool(monkeypatch, env, "export GIT_DIR=/live/.git", "a")
            assert tt.get_session_git_env("b") == _NONE_SET
            assert tt.get_session_git_env("a")["GIT_DIR"] is True
        finally:
            other.cleanup()

    def test_clear_session_cwd_drops_it(self):
        tt.record_session_git_env("gone", {**_NONE_SET, "GIT_DIR": True})
        tt.clear_session_cwd("gone")
        assert tt.get_session_git_env("gone") is None

    def test_non_dict_is_ignored(self):
        tt.record_session_git_env("s", {**_NONE_SET, "GIT_DIR": True})
        tt.record_session_git_env("s", None)
        assert tt.get_session_git_env("s")["GIT_DIR"] is True


class TestPreToolCallPayload:
    def test_payload_carries_session_git_env(self):
        from agent import shell_hooks

        task = "t20695198-hook"
        kwargs = {"tool_name": "terminal", "args": {"command": "git status"}, "task_id": task}
        assert "session_git_env" not in json.loads(shell_hooks._serialize_payload("pre_tool_call", kwargs))
        tt.record_session_git_env(task, _NONE_SET)
        assert json.loads(shell_hooks._serialize_payload("pre_tool_call", kwargs))["session_git_env"] == {}
        tt.record_session_git_env(task, {**_NONE_SET, "GIT_DIR": True, "GIT_WORK_TREE": True})
        payload = json.loads(shell_hooks._serialize_payload("pre_tool_call", kwargs))
        assert payload["session_git_env"] == {"GIT_DIR": True, "GIT_WORK_TREE": True}
        other = json.loads(shell_hooks._serialize_payload(
            "pre_tool_call", {**kwargs, "tool_name": "read_file"}))
        assert "session_git_env" not in other
        post = json.loads(shell_hooks._serialize_payload("post_tool_call", kwargs))
        assert "session_git_env" not in post
