"""`/overview` renders THIS chat's session card overview (t_66ffcd4f, W8-4).

Pins the registration (registry, gateway-known, dispatch table, mid-run dispatch) and the
handler contract: the invoking session id is passed to ``scripts/session-overview.py`` with
``--lineage``; ``fast`` adds ``--no-network``; every failure is a one-line reason, never a
raise and never an empty reply. The E2E arm runs a real script file under a temp root.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
import types

import pytest

from gateway import overview_command as oc
from hermes_cli.commands import GATEWAY_KNOWN_COMMANDS, resolve_command


class TestRegistration:
    def test_registered_gateway_only_and_dispatchable_mid_run(self):
        cmd = resolve_command("overview")
        assert cmd is not None and cmd.name == "overview"
        assert cmd.gateway_only and cmd.busy_policy == "dispatch"
        assert "overview" in GATEWAY_KNOWN_COMMANDS

    def test_plain_handler_table_routes_it(self):
        from gateway.run import GatewayRunner

        runner = object.__new__(GatewayRunner)
        table = GatewayRunner._gateway_plain_command_handlers(runner)
        assert table["overview"].__func__ is GatewayRunner._handle_overview_command


def _script(root, body):
    p = root / "scripts" / "session-overview.py"
    p.parent.mkdir(parents=True)
    p.write_text(textwrap.dedent(body))
    return p


class TestRender:
    def test_passes_the_invoking_session_with_lineage(self, tmp_path):
        _script(tmp_path, "")
        seen = {}

        def run(argv, **kw):
            seen["argv"] = argv
            return subprocess.CompletedProcess(argv, 0, "**Session overview** 18:00 PDT — 3 cards: done 3\n", "")

        out = oc.render_overview("sid_1", "", root=tmp_path, run=run, python="py")
        assert out.startswith("**Session overview**")
        assert seen["argv"] == ["py", str(tmp_path / "scripts" / "session-overview.py"), "sid_1", "--lineage"]

    def test_fast_skips_network(self, tmp_path):
        _script(tmp_path, "")
        seen = {}

        def run(argv, **kw):
            seen["argv"] = argv
            return subprocess.CompletedProcess(argv, 0, "x", "")

        oc.render_overview("sid_1", "fast", root=tmp_path, run=run, python="py")
        assert seen["argv"][-1] == "--no-network"

    @pytest.mark.parametrize("rc,stdout,stderr,want", [
        (2, "", "no born-here cards for sid_1", "no cards were born in this chat"),
        (2, "", "usage: session-overview.py ...\nsession-overview.py: error: unrecognized arguments: --lineage",
         "failed (rc=2): session-overview.py: error: unrecognized arguments: --lineage"),
        (1, "", "Traceback ...\nsqlite3.OperationalError: locked", "failed (rc=1): sqlite3.OperationalError: locked"),
        (0, "", "", "failed (rc=0)"),
    ])
    def test_failures_are_one_line_reasons(self, tmp_path, rc, stdout, stderr, want):
        _script(tmp_path, "")
        out = oc.render_overview("sid_1", root=tmp_path, python="py",
                                 run=lambda argv, **kw: subprocess.CompletedProcess(argv, rc, stdout, stderr))
        assert want in out and out.startswith("/overview:")

    def test_timeout_and_missing_script_and_no_session(self, tmp_path):
        def slow(argv, **kw):
            raise subprocess.TimeoutExpired(argv, kw["timeout"])

        assert "is not installed" in oc.render_overview("sid_1", root=tmp_path)
        _script(tmp_path, "")
        assert "did not finish in 5 s" in oc.render_overview("sid_1", root=tmp_path, run=slow, timeout=5)
        assert "no session yet" in oc.render_overview(None, root=tmp_path)

    def test_real_script_end_to_end(self, tmp_path):
        """Real subprocess, real argv: the script echoes what it was given."""
        _script(tmp_path, """
            import sys
            print("**Session overview** argv=" + " ".join(sys.argv[1:]))
        """)
        out = oc.render_overview("sid_9", "fast", root=tmp_path, python=sys.executable)
        assert out == "**Session overview** argv=sid_9 --lineage --no-network"


class TestHandler:
    @pytest.mark.asyncio
    async def test_handler_resolves_session_from_the_store(self, tmp_path, monkeypatch):
        from gateway.slash_commands import GatewaySlashCommandsMixin

        got = {}
        monkeypatch.setattr(oc, "render_overview", lambda sid, args="": got.update(sid=sid, args=args) or "OK")

        class Store:
            async def entry_for(self, key):
                got["key"] = key
                return types.SimpleNamespace(session_id="sid_live")

        mixin = object.__new__(GatewaySlashCommandsMixin)
        mixin.async_session_store = Store()
        mixin._session_key_for_source = lambda src: "agent:main:discord:group:1:2"
        event = types.SimpleNamespace(source=object(), get_command_args=lambda: "fast")
        assert await mixin._handle_overview_command(event) == "OK"
        assert got == {"key": "agent:main:discord:group:1:2", "sid": "sid_live", "args": "fast"}

    @pytest.mark.asyncio
    async def test_store_failure_is_not_reported_as_no_session(self, monkeypatch):
        """Prism P1 5e5486f95932: a failed lookup must not tell the user the chat has no session."""
        from gateway.slash_commands import GatewaySlashCommandsMixin

        called = []
        monkeypatch.setattr(oc, "render_overview", lambda *a, **k: called.append(a) or "OK")

        class Store:
            async def entry_for(self, key):
                raise RuntimeError("database is locked")

        mixin = object.__new__(GatewaySlashCommandsMixin)
        mixin.async_session_store = Store()
        mixin._session_key_for_source = lambda src: "k"
        event = types.SimpleNamespace(source=object(), get_command_args=lambda: "")
        out = await mixin._handle_overview_command(event)
        assert "could not resolve this chat's session (RuntimeError: database is locked)" in out
        assert "no session yet" not in out and not called


class TestProfileIsolation:
    """Prism P1 566b41b037f9: under multiplex the invoking profile is bound in ContextVars, not os.environ. The child
    must run with THAT profile's home and secrets; A -> B -> A on one process, observed from inside the child."""

    def test_child_env_follows_the_invoking_profile(self, tmp_path, monkeypatch):
        import json
        from agent.secret_scope import set_multiplex_active
        from hermes_constants import reset_hermes_home_override, set_hermes_home_override

        a = tmp_path / ".hermes"
        b = a / "profiles" / "b"
        b.mkdir(parents=True)
        (a / ".env").write_text("A_MARKER=a\nGH_TOKEN=a-gh\n", encoding="utf-8")
        (b / ".env").write_text("B_MARKER=b\nGH_TOKEN=b-gh\n", encoding="utf-8")
        monkeypatch.setenv("HERMES_HOME", str(a))
        monkeypatch.setenv("A_MARKER", "a")
        monkeypatch.setenv("GH_TOKEN", "a-gh")
        monkeypatch.delenv("B_MARKER", raising=False)
        _script(tmp_path, """
            import json, os
            print(json.dumps({k: os.environ.get(k) for k in ("HERMES_HOME", "A_MARKER", "B_MARKER", "GH_TOKEN")}))
        """)

        def seen():
            return json.loads(oc.render_overview("sid", root=tmp_path, python=sys.executable))

        set_multiplex_active(True)
        try:
            first = seen()                                   # A: the launch profile
            token = set_hermes_home_override(str(b))
            try:
                served = seen()                              # B: served profile
            finally:
                reset_hermes_home_override(token)
            again = seen()                                   # back to A
        finally:
            set_multiplex_active(False)
        assert served == {"HERMES_HOME": str(b), "A_MARKER": None, "B_MARKER": "b", "GH_TOKEN": "b-gh"}, served
        assert first["GH_TOKEN"] == again["GH_TOKEN"] == "a-gh" and first["B_MARKER"] is None
        assert first == again

    def test_unbuildable_env_is_reported_not_spawned(self, tmp_path):
        _script(tmp_path, "")
        ran = []

        def boom():
            raise RuntimeError("no profile scope bound")

        out = oc.render_overview("sid", root=tmp_path, python="py", child_env=boom,
                                 run=lambda *a, **k: ran.append(a))
        assert out.startswith("/overview: could not build this profile's child environment") and not ran
