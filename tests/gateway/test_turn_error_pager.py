"""A user-facing turn error pages the operator once per room episode (t_4cfe254a).

Replays the 2026-10-28 00:39 incident: Nick's Matrix room failed every turn with
SessionKeyConflict and only the user saw it. The real _process_message_background error
path runs against a fake transport; the pager is a real subprocess (a stand-in
episode_pager.py under the temp root that records its argv).
"""
import asyncio
import json
import sys
from unittest.mock import AsyncMock

import pytest

from gateway import turn_error_pager
from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import BasePlatformAdapter, SendResult
from gateway.platforms.event import MessageEvent
from gateway.routing_identity import SessionKeyConflict
from gateway.session import SessionSource

ROOM = "!Nx7SyVp7TzpGdLFuSJ9E92ohRtMDSB1t_LmpyEStkWY"

FAKE_PAGER = """\
import json, os, sys
with open(os.path.join(os.path.dirname(__file__), "calls.jsonl"), "a") as f:
    f.write(json.dumps(sys.argv[1:]) + "\\n")
if os.path.exists(os.path.join(os.path.dirname(__file__), "fail")):
    sys.exit(3)
print("paged" if sys.argv[1] == "page" else "resolved")
"""


class Adapter(BasePlatformAdapter):
    async def connect(self, **kwargs):
        return True

    async def disconnect(self):
        pass

    async def get_chat_info(self, chat_id):
        return {"name": "fixture", "type": "group"}

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        self.sent.append(content)
        return SendResult(success=True, message_id="sent")


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    lib = tmp_path / "scripts" / "lib"
    lib.mkdir(parents=True)
    (lib / "episode_pager.py").write_text(FAKE_PAGER)
    for handle in turn_error_pager._open_episodes.values():
        handle.cancel()
    turn_error_pager._open_episodes.clear()
    yield tmp_path
    for handle in turn_error_pager._open_episodes.values():
        handle.cancel()
    turn_error_pager._open_episodes.clear()


def _calls(home):
    path = home / "scripts" / "lib" / "calls.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def _adapter(platform):
    adapter = Adapter(PlatformConfig(enabled=True), platform)
    adapter.sent = []
    adapter._start_typing_refresh = lambda *a: None
    adapter._stop_typing_refresh = AsyncMock()
    adapter._run_processing_hook = AsyncMock()
    adapter._fire_post_delivery_callback = AsyncMock()
    adapter._flush_text_debounce_now = AsyncMock()
    adapter._finish_session_task = lambda *a: None
    return adapter


def _event(platform, chat_id=ROOM):
    source = SessionSource(platform=platform, chat_id=chat_id, chat_type="group",
                           user_id="@nickabarca:angventures.io", user_name="Nick Abarca")
    return MessageEvent(text="So ace wants me to order 2 parts", source=source)


def _conflict():
    return SessionKeyConflict(f"agent:main:matrix:dm:{ROOM}", f"agent:main:matrix:group:{ROOM}")


async def _drain():
    if turn_error_pager._inflight:
        await asyncio.gather(*list(turn_error_pager._inflight))
    await asyncio.sleep(0.01)  # let subprocess pipe-close callbacks run before the loop closes


async def _fail_turn(adapter, event, error):
    adapter._message_handler = AsyncMock(side_effect=error)
    await adapter._process_message_background(event, "session")
    await _drain()


@pytest.mark.asyncio
async def test_matrix_session_key_conflict_pages_once_per_episode(home, monkeypatch):
    adapter = _adapter(Platform.MATRIX)
    order = []
    real = turn_error_pager.maybe_page
    monkeypatch.setattr(turn_error_pager, "maybe_page",
                        lambda *a, **k: order.append(len(adapter.sent)) or real(*a, **k))
    error = _conflict()
    assert str(error) == f"session-key collision refused: matrix {ROOM} (dm vs group)"

    await _fail_turn(adapter, _event(Platform.MATRIX), error)

    assert len(adapter.sent) == 1 and "SessionKeyConflict" in adapter.sent[0]
    assert order == [1]  # the page is dispatched only after the user notice went out
    calls = _calls(home)
    assert len(calls) == 1
    argv = calls[0]
    assert argv[:5] == ["page", "--producer", "gateway-turn-error", "--key", f"matrix:{ROOM}"]
    text = argv[argv.index("--text") + 1]
    assert "matrix" in text and ROOM in text and "SessionKeyConflict" in text
    assert "Nick Abarca (@nickabarca:angventures.io)" in text
    assert "session-key collision refused" in text

    # Same room fails again inside the episode: user still told, no second page.
    await _fail_turn(adapter, _event(Platform.MATRIX), _conflict())
    assert len(adapter.sent) == 2
    assert len(_calls(home)) == 1

    # A different room is its own episode.
    await _fail_turn(adapter, _event(Platform.MATRIX, "!other:angventures.io"), _conflict())
    assert [c[4] for c in _calls(home)] == [f"matrix:{ROOM}", "matrix:!other:angventures.io"]


@pytest.mark.asyncio
async def test_episode_clears_after_quiet_then_repages(home, monkeypatch):
    monkeypatch.setattr(turn_error_pager, "QUIET_S", 0.05)
    adapter = _adapter(Platform.MATRIX)
    await _fail_turn(adapter, _event(Platform.MATRIX), _conflict())
    await asyncio.sleep(0.15)
    await _drain()
    monkeypatch.setattr(turn_error_pager, "QUIET_S", 3600)
    await _fail_turn(adapter, _event(Platform.MATRIX), _conflict())
    assert [c[0] for c in _calls(home)] == ["page", "clear", "page"]


@pytest.mark.asyncio
async def test_api_server_error_notifies_user_without_page(home):
    adapter = _adapter(Platform.API_SERVER)
    await _fail_turn(adapter, _event(Platform.API_SERVER, "req-1"), RuntimeError("boom"))
    assert len(adapter.sent) == 1
    assert _calls(home) == []


@pytest.mark.asyncio
async def test_knob_off_no_page(home):
    (home / "config.yaml").write_text("gateway:\n  page_on_turn_error: false\n")
    adapter = _adapter(Platform.MATRIX)
    await _fail_turn(adapter, _event(Platform.MATRIX), _conflict())
    assert len(adapter.sent) == 1
    assert _calls(home) == []


@pytest.mark.asyncio
async def test_pager_failure_is_logged_never_raised_and_retried(home, caplog):
    (home / "scripts" / "lib" / "fail").write_text("")
    adapter = _adapter(Platform.MATRIX)
    await _fail_turn(adapter, _event(Platform.MATRIX), _conflict())
    assert len(adapter.sent) == 1
    assert "turn-error pager ['page'" in caplog.text and "rc=3" in caplog.text
    # The failed page did not open an episode: the next error in the room retries it.
    await _fail_turn(adapter, _event(Platform.MATRIX), _conflict())
    assert [c[0] for c in _calls(home)] == ["page", "page"]


@pytest.mark.asyncio
async def test_pager_raising_in_process_still_delivers_notice(home, monkeypatch, caplog):
    def boom(*a, **k):
        raise RuntimeError("pager exploded")
    monkeypatch.setattr(turn_error_pager, "_dispatch", boom)
    adapter = _adapter(Platform.MATRIX)
    await _fail_turn(adapter, _event(Platform.MATRIX), _conflict())
    assert len(adapter.sent) == 1
    assert "pager exploded" in caplog.text
    assert turn_error_pager._open_episodes == {}


@pytest.mark.asyncio
async def test_missing_pager_script_is_a_logged_noop(home, caplog):
    (home / "scripts" / "lib" / "episode_pager.py").unlink()
    adapter = _adapter(Platform.DISCORD)
    await _fail_turn(adapter, _event(Platform.DISCORD, "123"), RuntimeError("boom"))
    assert len(adapter.sent) == 1
    assert "episode_pager.py not found" in caplog.text
