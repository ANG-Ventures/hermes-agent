"""Matrix room shape changes (dm <-> group) keep one conversation, and the store keys Matrix the
way the adapter does (per-platform ``group_sessions_per_user``).

Live incident 2026-10-28 00:39: room "Packaging materials quote" was created as a 2-member DM
(key ``agent:main:matrix:dm:!R``), a third member joined, and the next message from the original
member died with ``SessionKeyConflict (dm vs group)``, as did every later one. The store also keyed
the room per-user (``...:group:!R:@drew``) although ``matrix.group_sessions_per_user: false`` (one
shared conversation per room), because ``SessionStore._generate_session_key`` read only the global
flag while ``BasePlatformAdapter._source_session_key`` read the platform's.
"""
import json
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.base import BasePlatformAdapter
from gateway.routing_identity import SessionKeyConflict, assert_unique_routing_entries
from gateway.session import SessionEntry, SessionSource, SessionStore, build_session_key
from hermes_state import SessionDB

ROOM = "!Nx7room:angventures.io"
NICK, DREW = "@nickabarca:angventures.io", "@ajbartle88:angventures.io"
DM_KEY = f"agent:main:matrix:dm:{ROOM}"
GROUP_KEY = f"agent:main:matrix:group:{ROOM}"


def _config(group_per_user_extra=False):
    extra = {} if group_per_user_extra is None else {"group_sessions_per_user": group_per_user_extra}
    # Global stays at its default (True), as on the live host; the matrix extra says False.
    return GatewayConfig(platforms={Platform.MATRIX: PlatformConfig(enabled=True, token="t", extra=extra)})


def _src(chat_type, user=NICK):
    return SessionSource(platform=Platform.MATRIX, chat_id=ROOM, chat_type=chat_type, user_id=user)


def _store(tmp_path, config=None, backend="sqlite"):
    store = SessionStore(tmp_path, config or _config())
    store._db = SessionDB(tmp_path / "state.db") if backend == "sqlite" else None
    return store


def _seed(tmp_path, entries, backend="sqlite", config=None):
    """Pre-existing routes from an older release, written straight to disk (not via the guard)."""
    data = {e.session_key: e.to_dict() for e in entries}
    (tmp_path / "sessions.json").write_text(json.dumps(data))
    db = SessionDB(tmp_path / "state.db") if backend == "sqlite" else None
    if db:
        now = datetime.now().timestamp()
        db._execute_write(lambda conn: conn.executemany(
            "INSERT INTO gateway_routing(scope, session_key, entry_json, updated_at) VALUES (?, ?, ?, ?)",
            [(str(tmp_path.resolve()), k, json.dumps(v), now) for k, v in data.items()],
        ))
        for e in entries:
            db.create_session(e.session_id, source="matrix")
    store = SessionStore(tmp_path, config or _config())
    store._db = db
    return store


def test_routing_guard_refuses_a_second_type_for_one_matrix_room():
    """The card's repro: the guard is right to refuse; the store must never ask it to."""
    existing = {DM_KEY: {"origin": {"platform": "matrix", "chat_id": ROOM, "user_id": NICK}}}
    with pytest.raises(SessionKeyConflict):
        assert_unique_routing_entries(
            {GROUP_KEY: {"origin": {"platform": "matrix", "chat_id": ROOM, "user_id": NICK}}}, existing)


# --- defect (b): one key derivation for store and adapter -----------------------------------------

class _Adapter(BasePlatformAdapter):
    async def connect(self, *, is_reconnect=False):
        return True

    async def disconnect(self):
        return None

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        return None

    async def get_chat_info(self, chat_id):
        return {}


@pytest.mark.parametrize("extra", [False, True, None])
def test_store_and_adapter_produce_the_same_matrix_group_key(tmp_path, extra):
    config = _config(extra)
    store = _store(tmp_path, config, backend="json")
    adapter = _Adapter(config.platforms[Platform.MATRIX], Platform.MATRIX)
    source = _src("group", DREW)
    assert store._generate_session_key(source) == adapter._source_session_key(source)
    if extra is False:
        assert store._generate_session_key(source) == GROUP_KEY  # shared per room


def test_platform_extra_does_not_leak_into_other_platforms(tmp_path):
    config = _config(False)
    config.platforms[Platform.DISCORD] = PlatformConfig(enabled=True, token="t")
    store = _store(tmp_path, config, backend="json")
    src = SessionSource(platform=Platform.DISCORD, chat_id="1", chat_type="group", user_id="9")
    assert store._generate_session_key(src) == "agent:main:discord:group:1:9"


# --- defect (a): a room that changes shape keeps its conversation ---------------------------------

@pytest.mark.parametrize("backend", ["json", "sqlite"])
def test_dm_that_gains_a_member_continues_the_same_session(tmp_path, backend):
    store = _store(tmp_path, backend=backend)
    alerts = []
    store.on_session_key_conflict = alerts.append
    first = store.get_or_create_session(_src("dm"))
    assert first.session_key == DM_KEY

    # A third member joined: the adapter now labels the room ``group``.
    after = store.get_or_create_session(_src("group"))
    assert after.session_key == GROUP_KEY
    assert after.session_id == first.session_id
    assert after.origin.chat_type == "group"
    assert {e.session_key for e in store.snapshot_entries()} == {GROUP_KEY}
    assert alerts == []
    # Another member in the same (shared) room lands in the same conversation.
    assert store.get_or_create_session(_src("group", DREW)).session_id == first.session_id

    # Durable: a fresh load sees exactly the folded route.
    reloaded = SessionStore(tmp_path, _config())
    reloaded._db = store._db
    assert {e.session_key: e.session_id for e in reloaded.snapshot_entries()} == {GROUP_KEY: first.session_id}


def test_room_that_drops_back_to_two_members_folds_back_without_looping(tmp_path):
    store = _store(tmp_path)
    sid = store.get_or_create_session(_src("dm")).session_id
    assert store.get_or_create_session(_src("group")).session_id == sid
    back = store.get_or_create_session(_src("dm"))
    assert (back.session_key, back.session_id) == (DM_KEY, sid)
    assert {e.session_key for e in store.snapshot_entries()} == {DM_KEY}
    assert store.get_or_create_session(_src("dm")).session_id == sid  # stable, no re-fold


@pytest.mark.parametrize("backend", ["json", "sqlite"])
def test_live_incident_shape_dm_plus_per_user_group_row_is_answered(tmp_path, backend):
    """Exactly the live index: the old dm route + a per-user group row from before (b)."""
    now = datetime.now()
    dm = SessionEntry(session_key=DM_KEY, session_id="nick-dm", created_at=now, updated_at=now,
                      origin=_src("dm"))
    drew_key = f"{GROUP_KEY}:{DREW}"
    drew = SessionEntry(session_key=drew_key, session_id="drew-group", created_at=now,
                        updated_at=now + timedelta(hours=1), origin=_src("group", DREW))
    store = _seed(tmp_path, [dm, drew], backend)
    alerts = []
    store.on_session_key_conflict = alerts.append

    entry = store.get_or_create_session(_src("group"))  # Nick, after Drew joined

    assert entry.session_key == GROUP_KEY
    assert entry.session_id in {"nick-dm", "drew-group"}  # newest routing entry stays primary
    assert {e.session_key for e in store.snapshot_entries()} == {GROUP_KEY}
    assert alerts == []


def test_per_user_matrix_group_rows_fold_onto_the_room_key_at_load(tmp_path, caplog):
    now = datetime.now()
    other = "!other:angventures.io"
    rows = [
        SessionEntry(session_key=f"agent:main:matrix:group:{other}:{NICK}", session_id="older",
                     created_at=now, updated_at=now,
                     origin=SessionSource(platform=Platform.MATRIX, chat_id=other, chat_type="group", user_id=NICK)),
        SessionEntry(session_key=f"agent:main:matrix:group:{other}:{DREW}", session_id="newer",
                     created_at=now, updated_at=now + timedelta(minutes=1),
                     origin=SessionSource(platform=Platform.MATRIX, chat_id=other, chat_type="group", user_id=DREW)),
    ]
    store = _seed(tmp_path, rows)
    with caplog.at_level("WARNING"):
        snapshot = {e.session_key: e.session_id for e in store.snapshot_entries()}
    assert snapshot == {f"agent:main:matrix:group:{other}": "newer"}
    # With per-user isolation ON for matrix the same rows are canonical and stay untouched.
    on = tmp_path / "on"
    on.mkdir()
    store2 = _seed(on, rows, config=_config(True))
    assert {e.session_key for e in store2.snapshot_entries()} == {r.session_key for r in rows}


def test_dm_and_group_rows_of_one_room_are_not_folded_at_load(tmp_path):
    """Load only fixes isolation spelling; the type is the adapter's call on the next message."""
    now = datetime.now()
    dm = SessionEntry(session_key=DM_KEY, session_id="dm", created_at=now, updated_at=now, origin=_src("dm"))
    store = _seed(tmp_path, [dm])
    assert {e.session_key for e in store.snapshot_entries()} == {DM_KEY}


# --- end to end: GatewayRunner + MatrixAdapter ------------------------------------------------------

def _matrix_adapter(config, members):
    from plugins.platforms.matrix.adapter import MatrixAdapter

    adapter = MatrixAdapter(config.platforms[Platform.MATRIX])
    adapter._user_id = "@apollo:angventures.io"
    adapter._require_mention = False
    adapter._auto_thread = False
    adapter._matrix_session_scope = "room"
    adapter._background_read_receipt = MagicMock()
    adapter._get_display_name = AsyncMock(side_effect=lambda room, user: user)
    adapter._get_room_state_value = AsyncMock(return_value=None)
    adapter._get_room_member_count = AsyncMock(side_effect=lambda room: len(members))
    adapter._threads = MagicMock(mark_async=AsyncMock(), __contains__=lambda self, x: False)
    return adapter


@pytest.mark.asyncio
async def test_runner_answers_a_dm_that_gains_a_member_under_one_key(tmp_path, monkeypatch):
    from gateway.run import GatewayRunner

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    config = _config()
    config.sessions_dir = tmp_path / "sessions"
    runner = GatewayRunner(config)
    members = ["@apollo:angventures.io", NICK]
    adapter = _matrix_adapter(config, members)
    adapter.gateway_runner = runner
    adapter.set_session_store(runner.session_store)
    runner.adapters[Platform.MATRIX] = adapter
    store = runner.session_store

    async def route(sender, body):
        ctx = await adapter._resolve_message_context(
            ROOM, sender, f"${body}", body, {"body": body}, {})
        source = ctx[-1]
        adapter_key = adapter._source_session_key(source)
        entry = await runner.async_session_store.get_or_create_session(source)
        assert entry.session_key == adapter_key  # the adapter's guards and the store agree
        return entry

    first = await route(NICK, "hello")
    assert first.session_key == DM_KEY
    members.append(DREW)  # Drew joins: 3 members; the sync carrying the join drops the identity
    adapter._invalidate_room_identities(ROOM)
    from_drew = await route(DREW, "hi all")
    from_nick = await route(NICK, "still here")  # the message that died live
    assert from_drew.session_key == from_nick.session_key == GROUP_KEY
    assert from_drew.session_id == from_nick.session_id == first.session_id
    assert {e.session_key for e in store.snapshot_entries()} == {GROUP_KEY}
