"""Completed-card artifacts never upload to the lifecycle LOG channel, and a
held digest line never leaves an upload behind (t_4bfc46a3: 45 empty-content
uploads in #logs in 24 h). The files upload only beside a line posted now to a
conversation (the subscriber chat or an immediate home line); otherwise the
line names them. Real notifier ticks, config.yaml in the sandboxed HERMES_HOME,
session rows stubbed at ``kanban_home_route.read_session_row``."""
import asyncio
import json

import pytest
import yaml

from gateway import kanban_home_route as hr
from gateway.config import Platform
from gateway.platforms.base import BasePlatformAdapter
from gateway.run import GatewayRunner
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_notify as kbn
from hermes_constants import get_hermes_home

LOGS = "1480525090331561984"
APOLLO = "1502228850338435153"
SUB = "sub-chat"
S_APOLLO = "20260924_121319_4865b4f3"
SESSIONS = {
    S_APOLLO: {"origin_json": json.dumps({"platform": "discord", "chat_id": APOLLO}),
               "source": "discord"},
}


class Res:
    def __init__(self, ok=True):
        self.success, self.error, self.error_kind = ok, None if ok else "nope", None if ok else "forbidden"


class Adapter:
    extract_local_files = staticmethod(BasePlatformAdapter.extract_local_files)

    def __init__(self, fail_chats=()):
        self.sent, self.docs, self.handled = [], [], []
        self.fail_chats = set(fail_chats)

    async def send(self, chat_id, text, metadata=None):
        if chat_id in self.fail_chats:
            return Res(False)
        self.sent.append({"chat_id": chat_id, "text": text})
        return Res()

    async def send_document(self, chat_id, file_path, metadata=None, **kw):
        self.docs.append((chat_id, file_path.rsplit("/", 1)[-1]))

    async def send_video(self, chat_id, video_path, metadata=None, **kw):
        self.docs.append((chat_id, video_path.rsplit("/", 1)[-1]))

    async def send_multiple_images(self, chat_id, images, metadata=None, **kw):
        self.docs.extend((chat_id, u.rsplit("/", 1)[-1]) for u, _ in images)

    async def handle_message(self, event):
        self.handled.append(event)


@pytest.fixture(autouse=True)
def _sessions(monkeypatch):
    monkeypatch.setattr(hr, "read_session_row", lambda sid: SESSIONS.get(sid))
    # Upstream's media allowlist roots (_HERMES_ROOT/profiles, the cache-dir
    # SAFE_ROOTS) are frozen at import against the outer home; point them at
    # the isolated home so the home-IO guard does not refuse the path check.
    from gateway.platforms import base as _base
    monkeypatch.setattr(_base, "_HERMES_ROOT", get_hermes_home())
    monkeypatch.setattr(_base, "MEDIA_DELIVERY_SAFE_ROOTS", ())


async def _tick(monkeypatch, runner):
    real_sleep = asyncio.sleep

    async def fake_sleep(delay):
        if delay == 5:
            return None
        runner._running = False
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    await runner._kanban_notifier_watcher(interval=1)


def _run(tmp_path, monkeypatch, session, *, channel=True, home_digest=0, route=None,
         adapter=None):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "art.db"))
    kb.init_db()
    path = get_hermes_home() / "config.yaml"
    cfg = (yaml.safe_load(path.read_text()) if path.exists() else {}) or {}
    k = cfg.setdefault("kanban", {})
    k.update({"lifecycle_digest_seconds": 0, "lifecycle_home_digest_seconds": home_digest})
    if channel:
        k["lifecycle_channel"] = f"discord:{LOGS}"
    if route:
        k["lifecycle_route"] = route
    path.write_text(yaml.safe_dump(cfg))
    art = tmp_path / "out" / "native-ticks.txt"
    art.parent.mkdir()
    art.write_text("tick\n")
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="card", assignee="worker", session_id=session)
        kbn.add_notify_sub(conn, task_id=tid, platform="discord", chat_id=SUB,
                          chat_type="group", user_id="u1", delivery_mode="notify")
        kb.complete_task(conn, tid, summary="shipped", metadata={"artifacts": [str(art)]})
    ad = adapter or Adapter()
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._running = True
    runner.adapters = {Platform.DISCORD: ad}
    runner._kanban_sub_fail_counts = {}
    runner._kanban_dispatcher_lock_handle = object()
    asyncio.run(_tick(monkeypatch, runner))
    return tid, ad, runner


def _head(msg):
    return msg["text"].splitlines()[0]


def test_log_channel_line_names_artifacts_and_uploads_nothing(tmp_path, monkeypatch):
    tid, ad, _ = _run(tmp_path, monkeypatch, None)  # no home -> #logs [no-home]
    assert [m["chat_id"] for m in ad.sent] == [LOGS]
    assert "artifacts: native-ticks.txt" in _head(ad.sent[0]), ad.sent
    assert ad.docs == [], "no upload into the log channel"


def test_channel_route_mode_uploads_nothing_to_logs(tmp_path, monkeypatch):
    _tid, ad, _ = _run(tmp_path, monkeypatch, S_APOLLO, route="channel")
    assert [m["chat_id"] for m in ad.sent] == [LOGS]
    assert ad.docs == []


def test_immediate_home_line_carries_the_upload(tmp_path, monkeypatch):
    _tid, ad, _ = _run(tmp_path, monkeypatch, S_APOLLO)
    assert [m["chat_id"] for m in ad.sent] == [APOLLO]
    assert ad.docs == [(APOLLO, "native-ticks.txt")]
    assert "artifacts:" not in _head(ad.sent[0])


def test_held_home_digest_line_leaves_no_orphan_upload(tmp_path, monkeypatch):
    _tid, ad, runner = _run(tmp_path, monkeypatch, S_APOLLO, home_digest=120)
    assert ad.sent == [] and ad.docs == []
    asyncio.run(runner._kanban_lifecycle_digest.flush(0, force=True))
    assert [m["chat_id"] for m in ad.sent] == [APOLLO]
    assert "artifacts: native-ticks.txt" in _head(ad.sent[0])
    assert ad.docs == []


def test_home_unreachable_fallback_names_artifacts_no_upload(tmp_path, monkeypatch):
    _tid, ad, _ = _run(tmp_path, monkeypatch, S_APOLLO, adapter=Adapter(fail_chats={APOLLO}))
    assert [m["chat_id"] for m in ad.sent] == [LOGS]
    head = _head(ad.sent[0])
    assert "[home-unreachable:forbidden]" in head and "artifacts: native-ticks.txt" in head
    assert ad.docs == []


def test_no_lifecycle_channel_keeps_upload_in_subscriber_chat(tmp_path, monkeypatch):
    _tid, ad, _ = _run(tmp_path, monkeypatch, None, channel=False)
    assert [m["chat_id"] for m in ad.sent] == [SUB]
    assert ad.docs == [(SUB, "native-ticks.txt")]


def test_names_tag_caps_at_five():
    from gateway.kanban_watchers import kanban_artifact_names_tag

    assert kanban_artifact_names_tag(["/a/x.txt"]) == "artifacts: x.txt"
    tag = kanban_artifact_names_tag([f"/a/{i}.log" for i in range(7)])
    assert tag == "artifacts: 0.log, 1.log, 2.log, 3.log, 4.log +2 more"
