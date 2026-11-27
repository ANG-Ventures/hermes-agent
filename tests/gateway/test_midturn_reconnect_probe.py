"""Incident probe adapted to wire the real runner/installer. No network/model.
Original probe SHA256: 56c48803aae10a97ffc5ece2e887741b0887889bf8d74dfa9d529ebe3d17b429.
Transport retirement is injected at the watcher boundary (not a real socket fault).
"""
import asyncio
import queue
from contextlib import suppress
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
import pytest
from gateway.config import Platform
from gateway.platforms.base import SendResult
from gateway.run import GatewayRunner
from gateway.run_turn_runner import TurnRunner
from gateway.session import SessionSource
from gateway.turn_context import TurnContext
from plugins.platforms.discord.adapter import DiscordAdapter
from tests.gateway.test_run_progress_topics import ProgressCaptureAdapter

class Adapter(ProgressCaptureAdapter):
    send_typing = DiscordAdapter.send_typing
    stop_typing = DiscordAdapter.stop_typing
    _extract_discord_retry_after = staticmethod(DiscordAdapter._extract_discord_retry_after)

    def __init__(self):
        super().__init__(Platform.DISCORD)
        self._running = True
        self.dead = False
        self.posts = 0
        self.failed_edits = 0
        self.failed_sends = 0
        self._typing_tasks = {}
        self._typing_stop_events = {}
        self._typing_loop_token = {}
        async def request(route):
            self.posts += 1
        self._client = SimpleNamespace(is_closed=lambda: False, is_ready=lambda: True,
                                       http=SimpleNamespace(request=request))

    async def edit_message(self, chat_id, message_id, content):
        if self.dead:
            self.failed_edits += 1
            return SendResult(success=False, error='Session is closed')
        return await super().edit_message(chat_id, message_id, content)

    async def send(self, *args, **kwargs):
        if self.dead:
            self.failed_sends += 1
            return SendResult(success=False, error='Session is closed')
        return await super().send(*args, **kwargs)

async def settle(predicate, ticks=180):
    for _ in range(ticks):
        if predicate():
            return True
        await asyncio.sleep(.01)
    return bool(predicate())

def reconnect_runner(old, monkeypatch):
    runner = object.__new__(GatewayRunner)
    runner.adapters = {Platform.DISCORD: old}
    runner.delivery_router = SimpleNamespace(adapters=runner.adapters)
    runner._failed_platforms = {Platform.DISCORD: {'retired_adapter': old}}
    runner._sync_voice_mode_state_to_adapter = Mock()
    runner._bind_voice_input_callback = Mock()
    runner._update_platform_runtime_status = Mock()
    runner._schedule_planned_restart_replay = Mock()
    runner._redeliver_failed_obligations_for_platform = AsyncMock()
    runner._prepare_auto_resume_decisions = AsyncMock()
    runner._schedule_resume_pending_sessions = Mock()
    runner._delivery_adapter_for = lambda source: runner.adapters.get(source.platform)
    monkeypatch.setattr('gateway.channel_directory.build_channel_directory', AsyncMock())
    old.gateway_runner = runner
    return runner

async def install(runner, live):
    live.gateway_runner = runner
    await runner._install_reconnected_adapter(Platform.DISCORD, live)

@pytest.mark.asyncio
@pytest.mark.parametrize('variant', ['already_replaced', 'in_progress', 'connected_true'])
@pytest.mark.parametrize('aspect', ['progress', 'typing'])
async def test_turn_survives_adapter_replacement(variant, aspect, monkeypatch):
    import gateway.run_turn_runner as rtr
    import plugins.platforms.discord.adapter as da
    monkeypatch.setattr(da, '_DISCORD_TYPING_REFRESH_INTERVAL_SECONDS', .02)
    clock = SimpleNamespace(t=1000.)
    monkeypatch.setattr(rtr, 'time', SimpleNamespace(monotonic=lambda: clock.t))
    old, live = Adapter(), Adapter()
    runner = reconnect_runner(old, monkeypatch)
    source = SessionSource(platform=Platform.DISCORD, chat_id='123')
    ctx = TurnContext(source=source, session_key='probe', progress_queue=queue.Queue(),
                      progress_grouping='accumulate', _run_still_current=lambda: True)
    ctx.agent_holder = [SimpleNamespace(is_interrupted=False)]
    tr = TurnRunner(runner, ctx)
    token = old._issue_typing_token('123')
    refresh = asyncio.create_task(old._keep_typing('123', interval=.02, token=token))
    painter = asyncio.create_task(tr.send_progress_messages())
    produced = []
    async def tool(n):
        produced.append(n)
        clock.t += 10
        ctx.progress_queue.put('terminal: tool-' + str(n))
        await asyncio.sleep(.4)
    try:
        await tool(1)
        assert old.sent and await settle(lambda: old.posts > 0)
        old.dead = True
        # Real liveness watcher stamps fatal BEFORE closing; disconnect later sets
        # _running=False and _client=None. connected_true covers its pre-teardown window.
        # An unmarked-but-connected replacement is not a liveness reconnect:
        # adapter._liveness_watch sets fatal before it schedules client.close().
        old._set_fatal_error('discord_websocket_health_stale', 'latency_exceeded', retryable=True)
        old._running = variant == 'connected_true'
        old._client = None
        runner.adapters.pop(Platform.DISCORD)
        if variant != 'in_progress':
            await install(runner, live)
        await tool(2)
        if variant == 'in_progress':
            await install(runner, live)
        await tool(3)
        await tool(4)
        await asyncio.sleep(.5)
        assert produced == [1, 2, 3, 4] and not painter.done() and not refresh.done()
        print('RECEIPT', variant, aspect, 'old_fail_edits', old.failed_edits,
              'old_fail_sends', old.failed_sends, 'live_edits', len(live.edits),
              'live_typing_posts', live.posts)
        if aspect == 'progress':
            assert any('tool-4' in x['content'] for x in live.edits), 'live bubble never resumed'
        else:
            assert live.posts > 0, 'typing never re-armed on replacement adapter'
    finally:
        for task in (painter, refresh):
            task.cancel()
        for task in (painter, refresh):
            with suppress(asyncio.CancelledError):
                await task
        for adapter in (old, live):
            await adapter.stop_typing('123')

@pytest.mark.asyncio
async def test_base_refresh_alone_rebinds_to_live_adapter(monkeypatch):
    """No new progress event: refresh must independently survive reconnect."""
    import plugins.platforms.discord.adapter as da
    monkeypatch.setattr(da, '_DISCORD_TYPING_REFRESH_INTERVAL_SECONDS', .02)
    old, live = Adapter(), Adapter()
    runner = reconnect_runner(old, monkeypatch)
    task = asyncio.create_task(old._keep_typing('123', interval=.02,
                                             token=old._issue_typing_token('123')))
    try:
        assert await settle(lambda: old.posts > 0)
        old._set_fatal_error('discord_websocket_health_stale', 'latency_exceeded', retryable=True)
        old._running = False
        old._client = None
        runner.adapters.pop(Platform.DISCORD)
        await install(runner, live)
        await asyncio.sleep(.15)
        assert not task.done()
        assert live.posts > 0, 'base refresh remains bound to retired adapter indefinitely'
    finally:
        task.cancel()
        await task
        await old.stop_typing('123')
        await live.stop_typing('123')


@pytest.mark.asyncio
@pytest.mark.parametrize('finish', ['cancel', 'stop', 'supersede', 'second_reconnect'])
async def test_rebound_typing_ownership_and_cleanup(finish, monkeypatch):
    import plugins.platforms.discord.adapter as da
    monkeypatch.setattr(da, '_DISCORD_TYPING_REFRESH_INTERVAL_SECONDS', .01)
    old, live, newest = Adapter(), Adapter(), Adapter()
    runner = reconnect_runner(old, monkeypatch)
    stop = asyncio.Event()
    token = old._issue_typing_token('123')
    task = asyncio.create_task(old._keep_typing(
        '123', interval=.01, token=token, stop_event=stop, metadata={'thread_id': '123'}))
    try:
        assert await settle(lambda: old.posts > 0)
        old._set_fatal_error('discord_websocket_health_stale', 'stale', retryable=True)
        old._client = None
        await install(runner, live)
        assert await settle(lambda: live.posts > 0)
        assert live._current_typing_token('123') is not None
        if finish == 'second_reconnect':
            live._set_fatal_error('discord_websocket_health_stale', 'stale', retryable=True)
            live._client = None
            runner._failed_platforms[Platform.DISCORD] = {'retired_adapter': live}
            await install(runner, newest)
            assert await settle(lambda: newest.posts > 0)
            assert not live._typing_tasks and live._current_typing_token('123') is None
        if finish == 'supersede':
            newer_token = live._issue_typing_token('123')
            await live.stop_typing('123')
            await live.send_typing('123', token=newer_token)
            newer_loop = live._typing_tasks['123']
            await asyncio.wait_for(task, 2)
            assert live._current_typing_token('123') == newer_token
            assert live._typing_tasks['123'] is newer_loop and not newer_loop.done()
        else:
            if finish == 'stop':
                stop.set()
            else:
                task.cancel()
            await asyncio.wait_for(task, 2)
            for adapter in (live, newest):
                assert not adapter._typing_tasks
                assert not adapter._typing_loop_token
                assert adapter._current_typing_token('123') is None
    finally:
        task.cancel()
        await task
        for adapter in (old, live, newest):
            await adapter.stop_typing('123')


def test_typing_lookup_never_falls_back_from_secondary_to_primary():
    old, primary, live = Adapter(), Adapter(), Adapter()
    runner = SimpleNamespace(adapters={Platform.DISCORD: primary}, _profile_adapters={'secondary': {}})
    old.gateway_runner = runner
    old.set_owner_profile('secondary')
    assert old._live_typing_adapter() is None
    runner._profile_adapters['secondary'][Platform.DISCORD] = live
    assert old._live_typing_adapter() is live
