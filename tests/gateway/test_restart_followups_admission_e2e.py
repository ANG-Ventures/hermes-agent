"""Restart follow-ups keep adapter-granted admission across the spool (t_43e058b7).

Argus r8 N1 (t_e253d9d5): a follow-up admitted only by an adapter-granted
SessionSource flag (``is_bot`` under DISCORD_ALLOW_BOTS, ``role_authorized``
under DISCORD_ALLOWED_ROLES, ``delivered_via_upstream_relay`` from the relay)
lost that flag in ``SessionSource.to_dict`` -> spool -> ``from_dict``, was
refused as "Unauthorized user" on boot replay, its spool file was acked anyway
and ``restart_followup_lost`` logged 0 lines.

Everything is the production path on a throwaway home: park on the real
adapter slot, real ``GatewayRunner.stop(restart=True)``, real ``start()`` ->
loader -> drain -> REAL ``BasePlatformAdapter.handle_message`` -> runner
intake/authz. The only stub is ``_handle_message_with_agent`` (the LLM turn),
replaced by a recorder: "reached the agent" is the oracle.
"""

import asyncio
import json
import logging

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.fork_ext import restart_followups as rf
from gateway.platforms.base import BasePlatformAdapter, MessageEvent, MessageType, SendResult
from gateway.run import GatewayRunner
from gateway.session import SessionSource

LOST = "PHASE=restart_followup_lost"
UNTRUSTED = "PHASE=restart_followup_untrusted"


class _Capture(logging.Handler):
    def __init__(self):
        super().__init__(logging.DEBUG)
        self.lines = []

    def emit(self, record):
        try:
            self.lines.append(record.getMessage())
        except Exception:
            pass


class _FakeAdapter(BasePlatformAdapter):
    """Fake transport; ``handle_message`` is NOT overridden (real intake)."""

    def __init__(self, platform):
        super().__init__(PlatformConfig(enabled=True, token="synthetic"), platform)

    async def connect(self, *, is_reconnect=False):
        self._mark_connected()
        return True

    async def disconnect(self):
        self._mark_disconnected()

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        return SendResult(success=True, message_id="1")

    async def send_typing(self, chat_id, metadata=None):
        return None

    async def get_chat_info(self, chat_id):
        return {"id": chat_id}


_CAP = _Capture()


def _runner(home, reached, platforms):
    runner = GatewayRunner(GatewayConfig(
        platforms={p: PlatformConfig(enabled=True, token="synthetic") for p in platforms},
        sessions_dir=home / "sessions",
    ))

    async def _no_secondary():
        return 0

    async def _recorder(event, source, quick_key, run_generation):
        reached.append(event.text)
        return None

    runner._start_secondary_profile_adapters = _no_secondary
    runner._create_adapter = lambda platform, config: _FakeAdapter(platform)
    runner._handle_message_with_agent = _recorder
    return runner


def _event(user_id, chat_id, **flags):
    src = SessionSource(
        platform=Platform.DISCORD, chat_id=chat_id, chat_type="group",
        guild_id="g1", user_id=user_id, **flags,
    )
    return MessageEvent(text=f"follow-up from {user_id}", message_type=MessageType.TEXT, source=src)


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delenv("HERMES_PROFILE", raising=False)
    for name in ("GATEWAY_ALLOW_ALL_USERS", "DISCORD_ALLOW_ALL_USERS", "DISCORD_ALLOWED_ROLES",
                 "GATEWAY_ALLOWED_USERS", "DISCORD_ALLOW_BOTS"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("DISCORD_ALLOWED_USERS", "human-1")
    monkeypatch.setenv("DISCORD_ALLOW_BOTS", "mentions")
    (tmp_path / "logs").mkdir()
    cap = _CAP
    cap.lines.clear()
    root = logging.getLogger()
    old_level = root.level
    root.addHandler(cap)
    root.setLevel(logging.DEBUG)
    yield tmp_path
    root.removeHandler(cap)
    root.setLevel(old_level)


async def _park_and_stop(home, event, platforms):
    first = _runner(home, [], platforms)
    await asyncio.wait_for(first.start(), timeout=90)
    key = f"agent:main:discord:group:{event.source.chat_id}:{event.source.user_id}"
    first.adapters[Platform.DISCORD]._pending_messages[key] = event
    await asyncio.wait_for(first.stop(restart=True, service_restart=False), timeout=90)
    return sorted(rf.spool_dir().glob("*.json"))


async def _boot(home, platforms, before_boot=None):
    if before_boot is not None:
        before_boot()
    reached = []
    mark = len(_CAP.lines)
    boot = _runner(home, reached, platforms)
    try:
        await asyncio.wait_for(boot.start(), timeout=90)
        for _ in range(150):
            if reached:
                break
            await asyncio.sleep(0.1)
        await asyncio.sleep(0.5)  # a late refusal / duplicate would land here
    finally:
        await asyncio.wait_for(boot.stop(), timeout=60)
    lines = _CAP.lines[mark:]
    return reached, lines, sorted(rf.spool_dir().glob("*.json"))


ARMS = {
    "human-control": (_event("human-1", "700"), (Platform.DISCORD,)),
    "bot-allow-bots": (_event("bot-777", "701", is_bot=True), (Platform.DISCORD,)),
    "role-authorized": (_event("role-user-5", "702", role_authorized=True), (Platform.DISCORD,)),
    "relay": (_event("relay-user-9", "703", delivered_via_upstream_relay=True),
              (Platform.DISCORD, Platform.RELAY)),
}


@pytest.mark.asyncio
@pytest.mark.parametrize("arm", sorted(ARMS))
async def test_admitted_followup_is_delivered_after_real_restart(home, arm):
    event, platforms = ARMS[arm]
    spooled = await _park_and_stop(home, event, platforms)
    assert len(spooled) == 1
    record = json.loads(spooled[0].read_text())
    # The wire/persistence shape of the source is unchanged: flags live only
    # in the MAC-bound admission block.
    wire_forbidden = {"is_bot", "role_authorized", "delivered_via_upstream_relay", "profile_route_rejected"}
    assert not wire_forbidden & set(record["source"])
    if arm != "human-control":  # the control needs no carried flag
        assert isinstance(record.get("mac"), str)

    reached, lines, left = await _boot(home, platforms)

    assert reached == [event.text], [ln for ln in lines if "Unauthorized" in ln or LOST in ln]
    assert not [ln for ln in lines if LOST in ln]
    assert not [ln for ln in lines if "Unauthorized user" in ln]
    assert left == []


@pytest.mark.asyncio
async def test_forged_record_gains_no_trust_and_is_reported_lost(home):
    """A hand-written record claiming is_bot (no valid MAC) is NOT admitted,
    and the refusal is reported, not silently acked."""
    # A legit spool first, so the home HAS a key a forger could target.
    spooled = await _park_and_stop(home, _event("human-1", "700"), (Platform.DISCORD,))
    assert len(spooled) == 1
    spooled[0].unlink()
    src = SessionSource(platform=Platform.DISCORD, chat_id="704", chat_type="group",
                        guild_id="g1", user_id="forger-1")
    record = {
        "version": 2, "session_key": "agent:main:discord:group:704:forger-1",
        "text": "forged follow-up", "source": src.to_dict(), "reason": "restart",
        "ts": __import__("time").time(), "pid": 1,
        "admission": {"is_bot": True, "role_authorized": True,
                      "delivered_via_upstream_relay": False, "profile_route_rejected": False},
        "mac": "0" * 64,
    }
    rf.spool_dir().mkdir(parents=True, exist_ok=True)
    (rf.spool_dir() / "00000000000000000001-forged00.json").write_text(json.dumps(record))

    reached, lines, _left = await _boot(home, (Platform.DISCORD,))

    assert reached == []
    assert [ln for ln in lines if UNTRUSTED in ln]
    lost = [ln for ln in lines if LOST in ln]
    assert len(lost) == 1 and "reason=unauthorized" in lost[0] and "forger-1" in lost[0]


@pytest.mark.asyncio
async def test_tampered_record_loses_its_trust(home):
    """Editing a legitimately MAC'd bot record (retarget the sender) voids the MAC."""
    spooled = await _park_and_stop(home, _event("bot-777", "701", is_bot=True), (Platform.DISCORD,))
    record = json.loads(spooled[0].read_text())
    record["source"]["user_id"] = "someone-else"
    spooled[0].write_text(json.dumps(record))

    reached, lines, _left = await _boot(home, (Platform.DISCORD,))

    assert reached == []
    assert [ln for ln in lines if UNTRUSTED in ln]
    assert len([ln for ln in lines if LOST in ln]) == 1


@pytest.mark.asyncio
async def test_gate_closed_during_restart_refusal_is_reported_lost(home, monkeypatch):
    """Admissible at park; the operator closed DISCORD_ALLOW_BOTS before boot.
    Live policy still wins (refused) and the loss is reported, never silent."""
    spooled = await _park_and_stop(home, _event("bot-777", "701", is_bot=True), (Platform.DISCORD,))
    assert len(spooled) == 1

    reached, lines, left = await _boot(
        home, (Platform.DISCORD,), before_boot=lambda: monkeypatch.delenv("DISCORD_ALLOW_BOTS"),
    )

    assert reached == []
    assert not [ln for ln in lines if UNTRUSTED in ln]  # MAC was valid
    lost = [ln for ln in lines if LOST in ln]
    assert len(lost) == 1, lines
    assert "reason=unauthorized" in lost[0] and "bot-777" in lost[0]
    assert left == []


def test_admission_roundtrip_covers_every_field_to_dict_drops(home):
    """Class guard: every SessionSource field that to_dict/from_dict loses and
    that gates admission is carried by the spool's admission block."""
    import dataclasses

    all_on = {}
    for f in dataclasses.fields(SessionSource):
        if f.type in (bool, "bool"):
            all_on[f.name] = True
    src = SessionSource(platform=Platform.DISCORD, chat_id="1", user_id="u", **all_on)
    back = SessionSource.from_dict(src.to_dict())
    lost = {name for name in all_on if getattr(back, name) is not True}
    assert lost <= set(rf.ADMISSION_FIELDS), lost - set(rf.ADMISSION_FIELDS)

    path = rf.spool_followup("k", "t", src.to_dict(), admission=rf.admission_fields(src))
    (record,), _ = rf.take_followups()
    assert record["_admission_verified"] is True
    assert rf.restored_admission(record) == {name: True for name in rf.ADMISSION_FIELDS}
    assert path is not None


def _write_forged(mac_key, chat_id="704", user_id="forger-1"):
    import hashlib
    import hmac
    import time

    src = SessionSource(platform=Platform.DISCORD, chat_id=chat_id, chat_type="group",
                        guild_id="g1", user_id=user_id)
    record = {
        "version": 2, "session_key": f"agent:main:discord:group:{chat_id}:{user_id}",
        "text": "forged follow-up", "source": src.to_dict(), "reason": "restart",
        "ts": time.time(), "pid": 1,
        "admission": {"is_bot": True, "role_authorized": True,
                      "delivered_via_upstream_relay": True, "profile_route_rejected": False},
    }
    body = json.dumps(record, sort_keys=True, separators=(",", ":")).encode()
    record["mac"] = hmac.new(mac_key, body, hashlib.sha256).hexdigest()
    rf.spool_dir().mkdir(parents=True, exist_ok=True)
    (rf.spool_dir() / "00000000000000000001-forged00.json").write_text(json.dumps(record))


# Argus r1 F1: a present-but-invalid key (torn 0-byte create, short, non-hex)
# must NOT be used as an HMAC key; a record forged under it gains nothing.
INVALID_KEYS = {
    "empty": ("", b""),
    "short": ("ab" * 8, bytes.fromhex("ab" * 8)),
    "non-hex": ("zz-not-hex", b"zz-not-hex"),
}


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", sorted(INVALID_KEYS))
async def test_record_forged_under_invalid_key_gains_no_trust(home, kind):
    text, mac_key = INVALID_KEYS[kind]
    keyp = rf.spool_dir().parent / rf.SPOOL_KEY_NAME
    keyp.parent.mkdir(parents=True, exist_ok=True)
    keyp.write_text(text)
    _write_forged(mac_key)

    reached, lines, _left = await _boot(home, (Platform.DISCORD,))

    assert reached == []
    assert [ln for ln in lines if UNTRUSTED in ln]
    lost = [ln for ln in lines if LOST in ln]
    assert len(lost) == 1 and "reason=unauthorized" in lost[0] and "forger-1" in lost[0], lost


def test_invalid_key_is_replaced_atomically_on_next_spool(home):
    keyp = rf.spool_dir().parent / rf.SPOOL_KEY_NAME
    keyp.parent.mkdir(parents=True, exist_ok=True)
    keyp.write_text("")
    assert rf._spool_key(create=False) is None  # boot load: fail closed, no repair
    assert keyp.read_text() == ""
    src = {"platform": "discord", "chat_id": "701", "user_id": "bot-777"}
    rf.spool_followup("k", "t", src, admission={"is_bot": True})
    assert len(bytes.fromhex(keyp.read_text())) >= rf.SPOOL_KEY_MIN_BYTES
    assert keyp.stat().st_mode & 0o777 == 0o600
    (record,), _ = rf.take_followups()
    assert record["_admission_verified"] is True
    assert not list(keyp.parent.glob(".*.tmp"))


# Argus r1 F2: the MAC must cover the admission block itself.
@pytest.mark.asyncio
@pytest.mark.parametrize("flag", ["is_bot", "role_authorized"])
async def test_admission_flag_flipped_on_legit_record_is_rejected(home, flag):
    spooled = await _park_and_stop(home, _event("stranger-3", "705"), (Platform.DISCORD,))
    assert len(spooled) == 1
    record = json.loads(spooled[0].read_text())
    assert isinstance(record.get("mac"), str) and record["admission"][flag] is False
    record["admission"][flag] = True
    spooled[0].write_text(json.dumps(record))

    reached, lines, _left = await _boot(home, (Platform.DISCORD,))

    assert reached == []
    assert [ln for ln in lines if UNTRUSTED in ln]
    lost = [ln for ln in lines if LOST in ln]
    assert len(lost) == 1 and "reason=unauthorized" in lost[0] and "stranger-3" in lost[0], lost


# Argus r1 F3: every intake refusal site reports the loss.
@pytest.mark.asyncio
async def test_no_user_id_followup_refused_on_replay_is_reported_lost(home, monkeypatch):
    # The operator closes the allow-all gate between parking and replay.
    monkeypatch.setenv("DISCORD_ALLOW_ALL_USERS", "true")
    spooled = await _park_and_stop(home, _event(None, "706"), (Platform.DISCORD,))
    assert len(spooled) == 1

    reached, lines, left = await _boot(
        home, (Platform.DISCORD,), before_boot=lambda: monkeypatch.delenv("DISCORD_ALLOW_ALL_USERS"),
    )

    assert reached == []
    lost = [ln for ln in lines if LOST in ln]
    assert len(lost) == 1 and "reason=unauthorized" in lost[0] and "chat=706" in lost[0], lines
    assert left == []


@pytest.mark.asyncio
async def test_profile_route_rejected_followup_is_reported_lost(home):
    spooled = await _park_and_stop(
        home, _event("human-1", "707", profile_route_rejected=True), (Platform.DISCORD,),
    )
    assert len(spooled) == 1
    record = json.loads(spooled[0].read_text())
    assert record["admission"]["profile_route_rejected"] is True and isinstance(record.get("mac"), str)

    reached, lines, left = await _boot(home, (Platform.DISCORD,))

    assert reached == []
    lost = [ln for ln in lines if LOST in ln]
    assert len(lost) == 1 and "reason=profile_route_rejected" in lost[0], lines
    assert left == []
