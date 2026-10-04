"""GUI capability follows the SESSION's client, not the backend's process env.

The desktop app is a client. It can drive a backend that Electron spawned
locally, one reached over SSH, one behind a plain URL+token, or Hermes Cloud —
and only the first two run with ``HERMES_DESKTOP=1`` in their environment.
Gating the pane/browser/reaction tools on that env var therefore stripped every
one of them from URL and cloud gateways, while the same backend still told the
model "You are chatting inside the Hermes desktop app".

These tests pin the contract that replaced it: eligibility is resolved from the
session's own ``source`` (``session.create``'s ``source: 'desktop'``), so the
answer is identical on every connection topology.
"""

import pytest

import tui_gateway.server as server
from toolsets import TOOLSETS

GUI_TOOLS = {
    "annotate_preview",
    "desktop_preview",
    "drive_preview",
    "close_terminal",
    "focus_pane",
    "read_terminal",
    "read_window_below",
    "react_to_message",
    "show_tip",
    "gui_tour",
}


@pytest.fixture
def no_desktop_env(monkeypatch):
    """A backend nobody told about the desktop — i.e. every remote gateway."""
    monkeypatch.delenv("HERMES_DESKTOP", raising=False)
    monkeypatch.delenv("HERMES_DESKTOP_TERMINAL", raising=False)
    monkeypatch.delenv("HERMES_TUI_TOOLSETS", raising=False)
    return monkeypatch


class TestDesktopUiToolset:

    def test_stays_off_the_core_tool_list(self):
        """Core ships on every API call — a GUI-only tool must not be there."""
        from toolsets import _HERMES_CORE_TOOLS

        assert GUI_TOOLS.isdisjoint(_HERMES_CORE_TOOLS)

    def test_no_platform_bundle_carries_it(self):
        """Messaging/CLI bundles must not pick these up by listing them."""
        for name, spec in TOOLSETS.items():
            if name == "desktop_ui":
                continue
            assert GUI_TOOLS.isdisjoint(set(spec.get("tools") or ())), name


class TestSurfaceResolution:
    def test_desktop_session_gets_them_with_no_desktop_env(self, no_desktop_env):
        """THE regression: a desktop client on a remote/cloud backend."""
        assert "desktop_ui" in server._gui_surface_toolsets("desktop")

    def test_tui_session_does_not(self, no_desktop_env):
        assert "desktop_ui" not in server._gui_surface_toolsets("tui")

    def test_desktop_env_alone_does_not_grant_them(self, no_desktop_env):
        """A desktop-spawned backend serving a TUI session stays clean.

        The embedded terminal pane runs `hermes --tui` against this same
        backend; env-keyed gating handed it GUI tools it cannot answer.
        """
        no_desktop_env.setenv("HERMES_DESKTOP", "1")
        assert "desktop_ui" not in server._gui_surface_toolsets("tui")

    def test_project_tools_ride_on_every_gui_surface(self, no_desktop_env):
        for platform in ("desktop", "tui"):
            assert "project" in server._gui_surface_toolsets(platform)


class TestResolverPlumbing:
    def test_posture_path_folds_in_the_session_surface(self, no_desktop_env):
        """Focus-mode returns early — the surface toolsets must survive it."""
        import agent.coding_context as cc

        no_desktop_env.setattr(cc, "coding_selection", lambda **_: ["coding"])

        assert server._load_enabled_toolsets("desktop") == [
            "coding",
            "desktop_ui",
            "project",
        ]
        assert server._load_enabled_toolsets("tui") == ["coding", "project"]

    def test_config_path_folds_in_the_session_surface(self, no_desktop_env):
        import agent.coding_context as cc
        import hermes_cli.config as config_mod

        no_desktop_env.setattr(cc, "coding_selection", lambda **_: None)
        no_desktop_env.setattr(
            config_mod, "load_config", lambda: {"platform_toolsets": {"cli": ["memory"]}}
        )

        desktop = server._load_enabled_toolsets("desktop")
        tui = server._load_enabled_toolsets("tui")

        assert desktop is not None and tui is not None
        assert "desktop_ui" in desktop
        assert "desktop_ui" not in tui

    def test_explicit_env_pin_still_wins(self, no_desktop_env):
        """HERMES_TUI_TOOLSETS is an operator override; surface can't re-add."""
        no_desktop_env.setenv("HERMES_TUI_TOOLSETS", "web,memory")

        assert server._load_enabled_toolsets("desktop") == ["web", "memory"]


class TestDisabledToolsetsHonored:
    """``agent.disabled_toolsets`` must reach the final session tool list.

    The serve/TUI gateway folded ``project`` into every session and never
    forwarded the profile denylist to AIAgent, so a headless client
    (clanker-warm-client) carried ``desktop_project`` despite the profile
    (upstream #44499 / #54433; t_3f53bfd7).
    """

    @staticmethod
    def _cfg(disabled):
        return {
            "platform_toolsets": {"cli": ["todo"]},
            "agent": {"disabled_toolsets": disabled},
        }

    def _patch_cfg(self, mp, cfg):
        import agent.coding_context as cc
        import hermes_cli.config as config_mod

        mp.setattr(config_mod, "load_config", lambda: cfg)
        return cc

    def test_denylist_strips_fold_in_on_config_path(self, no_desktop_env):
        cc = self._patch_cfg(no_desktop_env, self._cfg(["project", "desktop_ui"]))
        no_desktop_env.setattr(cc, "coding_selection", lambda **_: None)

        for platform in ("clanker-warm-client", "tui", "desktop"):
            enabled = server._load_enabled_toolsets(platform)
            assert enabled is not None
            assert "project" not in enabled, platform
        # desktop_ui is the client's own control surface, not a model toolset.
        assert "desktop_ui" in server._load_enabled_toolsets("desktop")

    def test_denylist_strips_fold_in_on_posture_path(self, no_desktop_env):
        cc = self._patch_cfg(no_desktop_env, self._cfg(["project"]))
        no_desktop_env.setattr(cc, "coding_selection", lambda **_: ["coding"])

        assert server._load_enabled_toolsets("tui") == ["coding"]
        assert server._load_enabled_toolsets("desktop") == ["coding", "desktop_ui"]

    def test_no_denylist_keeps_fold_in(self, no_desktop_env):
        cc = self._patch_cfg(no_desktop_env, self._cfg([]))
        no_desktop_env.setattr(cc, "coding_selection", lambda **_: None)

        assert "project" in server._load_enabled_toolsets("clanker-warm-client")
        assert server._load_disabled_toolsets() is None

    def test_final_session_tools_exclude_denied(self, no_desktop_env):
        """Real resolver + real get_tool_definitions: the schema the model sees."""
        import model_tools

        cc = self._patch_cfg(no_desktop_env, self._cfg(["project"]))
        no_desktop_env.setattr(cc, "coding_selection", lambda **_: None)

        def names(disabled, **kw):
            defs = model_tools.get_tool_definitions(
                enabled_toolsets=server._load_enabled_toolsets("clanker-warm-client"),
                disabled_toolsets=disabled,
                quiet_mode=True,
                **kw,
            )
            return {d["function"]["name"] for d in defs}

        # Upstream renamed ``todo`` -> ``todo_list`` and defers it behind the tool_search bridge by
        # default, so the assembled schema is the three bridge tools; the denylist is proven on the
        # uncollapsed catalog (what tool_search/tool_call can reach) AND on the assembled schema.
        catalog = names(server._load_disabled_toolsets(), skip_tool_search_assembly=True)
        assert "todo_list" in catalog
        assert "desktop_project" not in catalog
        assert "desktop_project" not in names(server._load_disabled_toolsets())

    def test_make_agent_forwards_denylist(self, no_desktop_env):
        from unittest.mock import MagicMock, patch

        fake_runtime = {
            "provider": "anthropic", "base_url": "https://api.anthropic.com",
            "api_key": "sk-test-key", "api_mode": "anthropic_messages",
            "command": None, "args": None, "credential_pool": None,
        }
        with (
            patch("tui_gateway.server._load_cfg", return_value={"agent": {}}),
            patch("tui_gateway.server._get_db", return_value=MagicMock()),
            patch("tui_gateway.server._load_tool_progress_mode", return_value="compact"),
            patch("tui_gateway.server._load_reasoning_config", return_value=None),
            patch("tui_gateway.server._load_service_tier", return_value=None),
            patch("tui_gateway.server._load_enabled_toolsets", return_value=["todo", "project"]),
            patch("tui_gateway.server._load_disabled_toolsets", return_value=["project"]),
            patch(
                "hermes_cli.runtime_provider.resolve_runtime_provider",
                return_value=fake_runtime,
            ),
            patch("run_agent.AIAgent") as mock_agent,
        ):
            server._make_agent("sid-1", "key-1")

        assert mock_agent.call_args.kwargs["disabled_toolsets"] == ["project"]
