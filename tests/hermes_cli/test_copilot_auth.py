"""Tests for hermes_cli.copilot_auth — Copilot token validation and resolution."""

import pytest
from unittest.mock import patch


class TestTokenValidation:
    """Token type validation."""

    def test_classic_pat_rejected(self):
        from hermes_cli.copilot_auth import validate_copilot_token
        valid, msg = validate_copilot_token("ghp_abcdefghijklmnop1234")
        assert valid is False

    @pytest.mark.parametrize("token", ["gho_abcdefghijklmnop1234", "github_pat_abcdefghijklmnop1234", "ghu_abcdefghijklmnop1234"])
    def test_supported_token_families_accepted(self, token):
        from hermes_cli.copilot_auth import validate_copilot_token
        assert validate_copilot_token(token) == (True, "OK")

    def test_arbitrary_string_rejected(self):
        """A non-GitHub value in GITHUB_TOKEN must fail validation instead of reaching the API (#12650)."""
        from hermes_cli.copilot_auth import validate_copilot_token
        valid, msg = validate_copilot_token("not_a_github_token")
        assert valid is False


class TestResolveToken:
    """Token resolution with env var priority."""


    def test_gh_token_second_priority(self, monkeypatch):
        from hermes_cli.copilot_auth import resolve_copilot_token
        monkeypatch.delenv("COPILOT_GITHUB_TOKEN", raising=False)
        monkeypatch.setenv("GH_TOKEN", "gho_gh_second")
        monkeypatch.setenv("GITHUB_TOKEN", "gho_github_third")
        token, source = resolve_copilot_token()
        assert token == "gho_gh_second"
        assert source == "GH_TOKEN"


    def test_gh_cli_classic_pat_raises(self, monkeypatch):
        from hermes_cli.copilot_auth import resolve_copilot_token
        monkeypatch.delenv("COPILOT_GITHUB_TOKEN", raising=False)
        monkeypatch.delenv("GH_TOKEN", raising=False)
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        with patch("hermes_cli.copilot_auth._try_gh_cli_token", return_value="ghp_classic"):
            with pytest.raises(ValueError):
                resolve_copilot_token()

    def test_invalid_env_var_skips_gh_cli_fallback(self, monkeypatch):
        """When an env var is set but holds an unsupported classic PAT,
        resolve_copilot_token must NOT fall back to ``gh auth token``.

        The user explicitly exported a token; silently substituting one
        from the gh CLI credential store is surprising and the subprocess
        call adds up to 5s of latency on Windows cold starts (#60800).
        Only fall back to the CLI when NO Copilot env var is set at all.
        """
        from hermes_cli.copilot_auth import resolve_copilot_token
        monkeypatch.delenv("COPILOT_GITHUB_TOKEN", raising=False)
        monkeypatch.delenv("GH_TOKEN", raising=False)
        monkeypatch.setenv("GITHUB_TOKEN", "ghp_classic_pat_nope")
        with patch("hermes_cli.copilot_auth._try_gh_cli_token") as mock_cli:
            token, source = resolve_copilot_token()
        assert token == ""
        assert source == ""
        mock_cli.assert_not_called()

    def test_all_env_vars_invalid_skips_gh_cli_fallback(self, monkeypatch):
        """All three env vars set to classic PATs → no gh CLI call."""
        from hermes_cli.copilot_auth import resolve_copilot_token
        monkeypatch.setenv("COPILOT_GITHUB_TOKEN", "ghp_one")
        monkeypatch.setenv("GH_TOKEN", "ghp_two")
        monkeypatch.setenv("GITHUB_TOKEN", "ghp_three")
        with patch("hermes_cli.copilot_auth._try_gh_cli_token") as mock_cli:
            token, source = resolve_copilot_token()
        assert token == ""
        assert source == ""
        mock_cli.assert_not_called()


class TestGhShimSkip:
    """A gh-shim refuses `gh auth token` (I4): never probe through it.

    Regression: every agent start ran `gh auth token` via the shim and the
    intercept wrapper, logging 2-3 REFUSE lines each (194 in shim.log).
    """

    _MARKERS = ("HERMES_AGENT", "AI_AGENT", "HERMES_PROFILE", "HERMES_GH_LANE", "GH_SHIM_LANE", "GH_SHIM_REAL")

    def _env(self, **kw):
        return {"PATH": "/usr/bin", **kw}

    def _script(self, path, body):
        path.write_text("#!/bin/sh\n" + body)
        path.chmod(0o755)
        return str(path)

    def test_binary_in_gh_shim_dir_is_fronted(self, tmp_path):
        from hermes_cli.copilot_auth import gh_is_shim_fronted
        d = tmp_path / "var" / "gh-shim"
        d.mkdir(parents=True)
        gh = self._script(d / "gh", 'exec python3 gh-shim.py "$@"\n')
        assert gh_is_shim_fronted(gh, self._env()) is True
        assert gh_is_shim_fronted(gh, self._env(GH_SHIM_REAL="1")) is False

    def test_intercept_wrapper_fronted_only_with_agent_marker(self, tmp_path):
        from hermes_cli.copilot_auth import gh_is_shim_fronted
        gh = self._script(tmp_path / "gh", 'shim="$HOME/.hermes/var/gh-shim/gh"\nexec "$shim" "$@"\n')
        assert gh_is_shim_fronted(gh, self._env()) is False
        assert gh_is_shim_fronted(gh, self._env(HERMES_AGENT="0")) is False
        assert gh_is_shim_fronted(gh, self._env(HERMES_AGENT="1")) is True
        for marker in ("AI_AGENT", "HERMES_PROFILE", "HERMES_GH_LANE", "GH_SHIM_LANE"):
            assert gh_is_shim_fronted(gh, self._env(**{marker: "x"})) is True, marker
        assert gh_is_shim_fronted(gh, self._env(HERMES_PROFILE="x", GH_SHIM_REAL="1")) is False

    def test_plain_gh_is_not_fronted(self, tmp_path):
        from hermes_cli.copilot_auth import gh_is_shim_fronted
        gh = self._script(tmp_path / "gh", 'exec /usr/bin/true "$@"\n')
        binary = tmp_path / "gh-bin"
        binary.write_bytes(b"\x7fELF gh-shim")
        env = self._env(HERMES_PROFILE="x")
        assert gh_is_shim_fronted(gh, env) is False
        assert gh_is_shim_fronted(str(binary), env) is False
        assert gh_is_shim_fronted(None, env) is False
        assert gh_is_shim_fronted(str(tmp_path / "missing"), env) is False

    def test_probe_skips_shim_candidates_and_runs_real_ones(self, tmp_path, monkeypatch):
        from hermes_cli import copilot_auth
        for k in self._MARKERS:
            monkeypatch.delenv(k, raising=False)
        monkeypatch.setenv("HERMES_PROFILE", "worker")
        d = tmp_path / "gh-shim"
        d.mkdir()
        shim = self._script(d / "gh", "exit 3\n")
        wrapper = self._script(tmp_path / "wrapped-gh", 'exec "$HOME/.hermes/var/gh-shim/gh" "$@"\n')
        real = self._script(tmp_path / "real-gh", "echo gho_real\n")
        ran = []

        class R:
            returncode, stdout = 0, "gho_real\n"

        def fake_run(cmd, **kw):
            ran.append(cmd[0])
            return R()

        monkeypatch.setattr(copilot_auth, "_gh_cli_candidates", lambda: [shim, wrapper, real])
        monkeypatch.setattr(copilot_auth.subprocess, "run", fake_run)
        assert copilot_auth._probe_gh_cli_token() == "gho_real"
        assert ran == [real]

        monkeypatch.setattr(copilot_auth, "_gh_cli_candidates", lambda: [shim, wrapper])
        ran.clear()
        assert copilot_auth._probe_gh_cli_token() is None
        assert ran == []

    def test_env_token_still_resolves_under_shim(self, tmp_path, monkeypatch):
        from hermes_cli import copilot_auth
        d = tmp_path / "gh-shim"
        d.mkdir()
        shim = self._script(d / "gh", "exit 3\n")
        monkeypatch.setattr(copilot_auth, "_gh_cli_candidates", lambda: [shim])
        monkeypatch.delenv("COPILOT_GITHUB_TOKEN", raising=False)
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        monkeypatch.setenv("GH_TOKEN", "gho_from_env")
        with patch.object(copilot_auth.subprocess, "run") as run:
            assert copilot_auth.resolve_copilot_token() == ("gho_from_env", "GH_TOKEN")
        run.assert_not_called()

    def test_skills_hub_skips_gh_cli_under_shim(self, tmp_path, monkeypatch):
        from tools import skills_hub_github as skills_hub  # GitHubAuth's home after upstream's hub split
        d = tmp_path / "gh-shim"
        d.mkdir()
        shim = self._script(d / "gh", "exit 3\n")
        monkeypatch.delenv("GH_SHIM_REAL", raising=False)
        monkeypatch.setattr(skills_hub.shutil, "which", lambda name: shim)
        with patch.object(skills_hub.subprocess, "run") as run:
            assert skills_hub.GitHubAuth()._try_gh_cli() is None
        run.assert_not_called()


class TestGhCliTokenCache:
    """The gh-CLI probe result is cached — a miss must not re-spawn gh.

    Regression: /api/model/options ran `gh auth token` four times per build;
    with no gh credential store each probe blocked its full 5s timeout, so the
    Desktop Models/Providers settings pages took 20s per open and exceeded the
    renderer's 15s IPC budget (Aug 2026 desktop audit).
    """

    def _reset(self):
        from hermes_cli.copilot_auth import _invalidate_gh_cli_token_cache
        _invalidate_gh_cli_token_cache()

    def test_miss_is_cached_and_probe_runs_once(self):
        from hermes_cli import copilot_auth
        self._reset()
        with patch.object(copilot_auth, "_probe_gh_cli_token", return_value=None) as probe:
            assert copilot_auth._try_gh_cli_token() is None
            assert copilot_auth._try_gh_cli_token() is None
            assert copilot_auth._try_gh_cli_token() is None
        assert probe.call_count == 1
        self._reset()


    def test_ttl_expiry_reprobes(self, monkeypatch):
        from hermes_cli import copilot_auth
        self._reset()
        clock = {"now": 1000.0}
        monkeypatch.setattr(copilot_auth.time, "monotonic", lambda: clock["now"])
        with patch.object(copilot_auth, "_probe_gh_cli_token", return_value=None) as probe:
            copilot_auth._try_gh_cli_token()
            clock["now"] += copilot_auth._GH_CLI_TOKEN_CACHE_TTL_SECONDS + 1
            copilot_auth._try_gh_cli_token()
        assert probe.call_count == 2
        self._reset()


class TestRequestHeaders:
    """Copilot API header generation."""

    def test_default_headers_include_openai_intent(self):
        from hermes_cli.copilot_auth import copilot_request_headers
        headers = copilot_request_headers()
        assert headers["Openai-Intent"] == "conversation-edits"
        assert headers["User-Agent"] == "HermesAgent/1.0"
        assert "Editor-Version" in headers


    def test_no_vision_header_by_default(self):
        from hermes_cli.copilot_auth import copilot_request_headers
        headers = copilot_request_headers()
        assert "Copilot-Vision-Request" not in headers


class TestCopilotDefaultHeaders:
    """The models.py copilot_default_headers uses copilot_auth."""


    def test_param_passthrough_both_values(self):
        """is_agent_turn param correctly maps to x-initiator for both True and False."""
        from hermes_cli.models import copilot_default_headers
        for is_agent, expected in [(True, "agent"), (False, "user")]:
            headers = copilot_default_headers(is_agent_turn=is_agent)
            assert headers["x-initiator"] == expected, (
                f"is_agent_turn={is_agent} should produce x-initiator={expected!r}, "
                f"got {headers['x-initiator']!r}"
            )


class TestEnvVarOrder:
    """PROVIDER_REGISTRY has correct env var order."""

    def test_copilot_env_vars_include_copilot_github_token(self):
        from hermes_cli.auth import PROVIDER_REGISTRY
        copilot = PROVIDER_REGISTRY["copilot"]
        assert "COPILOT_GITHUB_TOKEN" in copilot.api_key_env_vars
        # COPILOT_GITHUB_TOKEN should be first
        assert copilot.api_key_env_vars[0] == "COPILOT_GITHUB_TOKEN"

