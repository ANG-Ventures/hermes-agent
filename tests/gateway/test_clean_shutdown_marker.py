"""Tests for the clean shutdown marker that prevents unwanted session auto-resets.

When the gateway shuts down gracefully (hermes update, gateway restart, /restart),
it writes a .clean_shutdown marker.  On the next startup, if the marker exists,
crash-turn recovery is skipped and orphan turn markers are discarded.
"""

from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch


from gateway.config import GatewayConfig, Platform
from gateway.session import SessionSource, SessionStore


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_source(platform=Platform.TELEGRAM, chat_id="123", user_id="u1"):
    return SessionSource(platform=platform, chat_id=chat_id, user_id=user_id)


def _make_store(tmp_path):
    config = GatewayConfig()
    return SessionStore(sessions_dir=tmp_path, config=config)


# ---------------------------------------------------------------------------
# Clean shutdown marker integration
# ---------------------------------------------------------------------------

class TestCleanShutdownMarker:
    """Test that the marker file controls session suspension on startup."""

    def test_marker_written_on_graceful_stop(self, tmp_path, monkeypatch):
        """stop() should write .clean_shutdown marker."""
        monkeypatch.setattr("gateway.run._hermes_home", tmp_path)
        marker = tmp_path / ".clean_shutdown"
        assert not marker.exists()

        # Create a minimal runner and call the shutdown logic directly
        from gateway.run import GatewayRunner
        runner = object.__new__(GatewayRunner)
        runner._restart_requested = False
        runner._restart_detached = False
        runner._restart_via_service = False
        runner._restart_task_started = False
        runner._running = True
        runner._draining = False
        runner._stop_task = None
        runner._running_agents = {}
        runner._pending_messages = {}
        runner._pending_approvals = {}
        runner._background_tasks = set()
        runner._shutdown_event = MagicMock()
        runner._restart_drain_timeout = 5
        runner._exit_code = None
        runner._exit_reason = None
        runner.adapters = {}
        runner.config = GatewayConfig()

        # Mock heavy dependencies
        with patch("gateway.run.GatewayRunner._drain_active_agents", new_callable=AsyncMock, return_value=([], False)), \
             patch("gateway.run.GatewayRunner._finalize_shutdown_agents"), \
             patch("gateway.run.GatewayRunner._update_runtime_status"), \
             patch("gateway.status.remove_pid_file"), \
             patch("tools.process_registry.process_registry") as mock_proc_reg, \
             patch("tools.terminal_tool.cleanup_all_environments"), \
             patch("tools.browser_tool_lifecycle.cleanup_all_browsers"):
            mock_proc_reg.kill_all = MagicMock()

            import asyncio
            asyncio.get_event_loop().run_until_complete(runner.stop())

        assert marker.exists(), ".clean_shutdown marker should exist after graceful stop"


    def test_marker_written_when_only_cron_work_outlives_the_drain(self, tmp_path, monkeypatch):
        """A cron job past its own drain deadline is terminated and recorded in
        jobs.json — but no chat/api turn was interrupted, so the next boot has
        nothing to recover. The marker MUST still be written. Without it the
        boot runs ``suspend_recently_active()`` and re-marks every session that
        was active in the last 120s (measured 2026-09-20 00:22:34:
        ``active_at_start=0 active_now=0 cron_now=1`` -> marker skipped ->
        "Marked 4 in-flight session(s) as resumable" -> four finished sessions
        re-prompted). Drives the REAL ``stop()`` drain, no LLM, no adapters."""
        import asyncio
        import cron.scheduler as sched
        import tools.process_registry as _pr
        import tools.terminal_tool as _tt
        import tools.browser_tool_lifecycle as _bt
        from tests.gateway.restart_test_helpers import make_restart_runner

        monkeypatch.setattr("gateway.run._hermes_home", tmp_path)
        marker = tmp_path / ".clean_shutdown"
        runner, adapter = make_restart_runner()
        runner._restart_drain_timeout = 0.01
        runner._cron_drain_timeout = 0.01  # past the cron floor too (#82161)
        adapter.disconnect = AsyncMock()
        # Upstream keys in-flight cron state by (home key, job id).
        job_key = sched._inflight_key("job-1")
        sched._running_job_ids.add(job_key)
        sched._running_fire_owners[job_key] = {
            object(): ("owner-1", sched._get_hermes_home().resolve())
        }
        monkeypatch.setattr(_pr.process_registry, "kill_all", lambda *a, **k: 0)
        monkeypatch.setattr(_tt, "cleanup_all_environments", lambda: None)
        monkeypatch.setattr(_bt, "cleanup_all_browsers", lambda: None)
        try:
            with patch("gateway.status.remove_pid_file"), \
                 patch("gateway.status.write_runtime_status"), \
                 patch("cron.scheduler.mark_job_run"):
                asyncio.run(runner.stop())
        finally:
            sched._running_job_ids.discard(job_key)
            sched._running_fire_owners.pop(job_key, None)
        assert marker.exists(), (
            ".clean_shutdown must be written when the only work that outlived "
            "the drain was a cron job — no chat session was interrupted"
        )

    def test_marker_skipped_when_a_chat_turn_outlives_the_drain(self, tmp_path, monkeypatch):
        """Negative control for the test above: a chat turn that is still
        running at the deadline IS interrupted, its transcript may be half
        finished, and the marker must stay absent so the boot recovers it."""
        import asyncio
        from tests.gateway.restart_test_helpers import make_restart_runner

        monkeypatch.setattr("gateway.run._hermes_home", tmp_path)
        marker = tmp_path / ".clean_shutdown"
        runner, adapter = make_restart_runner()
        runner._restart_drain_timeout = 0.05
        adapter.disconnect = AsyncMock()
        runner._running_agents = {"agent:main:telegram:dm:1": MagicMock()}  # never finishes
        store = MagicMock()
        store._entries = {}
        store.mark_resume_pending = MagicMock(return_value=True)
        store.clear_resume_pending = MagicMock(return_value=True)
        runner.session_store = store
        with patch("gateway.status.remove_pid_file"), \
             patch("gateway.status.write_runtime_status"):
            asyncio.run(runner.stop())
        assert not marker.exists(), (
            "a genuinely interrupted chat turn must suppress the marker"
        )


# ---------------------------------------------------------------------------
# resume_pending freshness gate (#46934)
# ---------------------------------------------------------------------------
