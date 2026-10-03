"""Kanban lifecycle lines route to the card's HOME channel; the lifecycle
channel (#logs) is the fallback only (t_808bc8e6, Ace ruling 2026-10-02 17:30).

Driven through config.yaml in the sandboxed HERMES_HOME and real notifier
ticks. Session rows are stubbed at ``kanban_home_route.read_session_row`` (the
one state.db read)."""
import argparse
import asyncio
import json

import pytest
import yaml

from gateway import kanban_home_route as hr
from gateway.config import Platform
from gateway.run import GatewayRunner
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_notify as kbn
from hermes_constants import get_hermes_home

LOGS = "1480525090331561984"     # #logs (kanban.lifecycle_channel)
APOLLO = "1502228850338435153"   # #apollo: the Apollo main session's chat
KANBAN_CC = "1553871972885078087"
TG_ACE = "571820863"
S_APOLLO = "20260924_121319_4865b4f3"
S_CC = "20260927_135358_92971ac4"
S_TG = "20260928_182239_3e9f9fd7"
S_NOCHAN = "20261002_cli_000000"


def _origin(platform, chat_id, **kw):
    return {"origin_json": json.dumps({"platform": platform, "chat_id": chat_id, **kw}),
            "source": platform}


SESSIONS = {
    S_APOLLO: _origin("discord", APOLLO, chat_type="group", profile="default"),
    S_CC: _origin("discord", KANBAN_CC, chat_name="Daemonarchy / #kanban-cc", chat_type="group"),
    S_TG: _origin("telegram", TG_ACE, chat_type="dm"),
    S_NOCHAN: {"origin_json": None, "source": "cli"},
}


class Res:
    def __init__(self, ok=True, kind=None):
        self.success, self.error, self.error_kind = ok, None if ok else "nope", kind


class Adapter:
    def __init__(self, fail_chats=()):
        self.sent, self.handled, self.fail_chats = [], [], set(fail_chats)

    async def send(self, chat_id, text, metadata=None):
        if chat_id in self.fail_chats:
            return Res(False, "forbidden")
        self.sent.append({"chat_id": chat_id, "text": text, "metadata": metadata})
        return Res()

    async def handle_message(self, event):
        self.handled.append(event)


@pytest.fixture(autouse=True)
def _sessions(monkeypatch):
    calls = []

    def fake(sid):
        calls.append(sid)
        return SESSIONS.get(sid)

    monkeypatch.setattr(hr, "read_session_row", fake)
    return calls


def _cfg(**kv):
    path = get_hermes_home() / "config.yaml"
    cfg = (yaml.safe_load(path.read_text()) if path.exists() else {}) or {}
    cfg.setdefault("kanban", {}).update(kv)
    path.write_text(yaml.safe_dump(cfg))


async def _tick(monkeypatch, runner):
    real_sleep = asyncio.sleep

    async def fake_sleep(delay):
        if delay == 5:
            return None
        runner._running = False
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    await runner._kanban_notifier_watcher(interval=1)


def _runner(adapters):
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._running = True
    runner.adapters = adapters
    runner._kanban_sub_fail_counts = {}
    runner._kanban_dispatcher_lock_handle = object()
    return runner


def _card(conn, session=None, title="card", sub_chat=APOLLO, assignee="worker"):
    tid = kb.create_task(conn, title=title, assignee=assignee, session_id=session)
    kbn.add_notify_sub(conn, task_id=tid, platform="discord", chat_id=sub_chat,
                      chat_type="group", user_id="u1", delivery_mode="notify")
    return tid


def _run(tmp_path, monkeypatch, make, *, digest=0, home_digest=0, route=None,
         discord=None, telegram=None, clock=None):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "hr.db"))
    kb.init_db()
    cfg = {"lifecycle_channel": f"discord:{LOGS}", "lifecycle_digest_seconds": digest,
           "lifecycle_home_digest_seconds": home_digest}
    if route:
        cfg["lifecycle_route"] = route
    _cfg(**cfg)
    with kb.connect_closing() as conn:
        ids = make(conn)
    adapters = {Platform.DISCORD: discord or Adapter()}
    if telegram is not None:
        adapters[Platform.TELEGRAM] = telegram
    runner = _runner(adapters)
    if clock is not None:
        import gateway.kanban_watchers as kw

        monkeypatch.setattr(kw.time, "time", lambda: clock[0])
    asyncio.run(_tick(monkeypatch, runner))
    return ids, adapters, runner


# --- resolver ---------------------------------------------------------------


def test_resolver_home_nohome_and_reasons():
    assert hr.home_from_row(S_APOLLO, SESSIONS[S_APOLLO]).target == ("discord", APOLLO, "")
    assert hr.home_from_row(None, None).reason == "no-session"
    assert hr.home_from_row("operator:apollo", None).reason == "operator-home"
    assert hr.home_from_row("gone", None).reason == "unknown-session"
    assert hr.home_from_row(S_NOCHAN, SESSIONS[S_NOCHAN]).reason == "no-channel"
    tui = _origin("tui", "k")
    assert hr.home_from_row("s", tui).target is None
    # A Discord thread session: the thread IS the chat (no separate thread_id).
    th = _origin("discord", "999", thread_id="999")
    assert hr.home_from_row("s", th).target == ("discord", "999", "")
    tg_topic = _origin("telegram", "-100", thread_id="7")
    assert hr.home_from_row("s", tg_topic).target == ("telegram", "-100", "7")


def test_home_cache_is_per_card_and_rehome_misses(_sessions):
    c = hr.HomeCache()
    c.get("default", "t_1", S_APOLLO, now=0)
    c.get("default", "t_1", S_APOLLO, now=5)
    assert _sessions == [S_APOLLO], "second read of the same card is cached"
    c.get("default", "t_1", S_CC, now=6)        # re-homed: new session -> miss
    c.get("default", "t_1", S_CC, now=6 + hr.HOME_CACHE_TTL)  # TTL expiry -> re-read
    assert _sessions == [S_APOLLO, S_CC, S_CC]
    c.get("default", "t_2", "operator:apollo", now=7)
    assert len(_sessions) == 3, "operator homes never hit state.db"


# --- routing ----------------------------------------------------------------


def test_done_line_lands_in_home_not_logs(tmp_path, monkeypatch):
    def make(conn):
        tid = _card(conn, S_APOLLO)
        kb.complete_task(conn, tid, summary="shipped")
        return tid

    tid, ad, _ = _run(tmp_path, monkeypatch, make)
    sent = ad[Platform.DISCORD].sent
    assert [m["chat_id"] for m in sent] == [APOLLO], sent
    assert f"{tid} done" in sent[0]["text"] and "[no-home]" not in sent[0]["text"]


def test_card_homed_elsewhere_follows_its_home_not_the_subscriber(tmp_path, monkeypatch):
    def make(conn):
        tid = _card(conn, S_CC, sub_chat=APOLLO)
        kb.request_review(conn, tid, summary="PR ready", force=True)
        return tid

    _tid, ad, _ = _run(tmp_path, monkeypatch, make)
    assert [m["chat_id"] for m in ad[Platform.DISCORD].sent] == [KANBAN_CC]


@pytest.mark.parametrize("session", [None, "operator:apollo", S_NOCHAN, "gone"])
def test_no_home_falls_back_to_logs_tagged(tmp_path, monkeypatch, session):
    def make(conn):
        tid = _card(conn, session)
        kb.complete_task(conn, tid, summary="shipped")
        return tid

    tid, ad, _ = _run(tmp_path, monkeypatch, make)
    sent = ad[Platform.DISCORD].sent
    assert [m["chat_id"] for m in sent] == [LOGS]
    assert sent[0]["text"].splitlines()[0].endswith("[no-home]"), sent[0]["text"]


def test_home_send_failure_falls_back_to_logs_with_reason(tmp_path, monkeypatch):
    def make(conn):
        tid = _card(conn, S_APOLLO)
        kb.block_task(conn, tid, reason="waiting", kind="dependency")
        return tid

    tid, ad, runner = _run(tmp_path, monkeypatch, make, discord=Adapter(fail_chats={APOLLO}))
    sent = ad[Platform.DISCORD].sent
    assert [m["chat_id"] for m in sent] == [LOGS]
    assert "[home-unreachable:forbidden]" in sent[0]["text"].splitlines()[0]
    assert not runner._kanban_sub_fail_counts, "a delivered fallback is a delivery"


def test_home_platform_without_adapter_falls_back(tmp_path, monkeypatch):
    def make(conn):
        tid = _card(conn, S_TG)
        kb.complete_task(conn, tid, summary="shipped")
        return tid

    _tid, ad, _ = _run(tmp_path, monkeypatch, make)  # no telegram adapter on this gateway
    sent = ad[Platform.DISCORD].sent
    assert [m["chat_id"] for m in sent] == [LOGS]
    assert "[home-unreachable:no-adapter]" in sent[0]["text"]


def test_telegram_home_strips_headers_discord_wraps_links():
    assert hr.format_for_platform("telegram", "## Title\nbody") == "Title\nbody"
    out = hr.format_for_platform("discord", "PR https://github.com/o/r/pull/1 and <https://x.y>")
    assert out == "PR <https://github.com/o/r/pull/1> and <https://x.y>"


def test_telegram_home_receives_line_without_headers(tmp_path, monkeypatch):
    def make(conn):
        tid = _card(conn, S_TG)
        kb.complete_task(conn, tid, summary="## Shipped\nmore")
        return tid

    tg = Adapter()
    _tid, ad, _ = _run(tmp_path, monkeypatch, make, telegram=tg)
    assert [m["chat_id"] for m in tg.sent] == [TG_ACE]
    assert "##" not in tg.sent[0]["text"]


def test_changes_requested_goes_home_and_stays_with_subscriber_when_homeless(tmp_path, monkeypatch):
    def make(conn):
        a = _card(conn, S_CC, sub_chat=APOLLO)
        b = _card(conn, None, sub_chat=APOLLO)
        with kb.write_txn(conn):
            for t in (a, b):
                kb._append_event(conn, t, "changes_requested",
                                 {"reason": "fix the test", "reviewer": "apollo"})
        return a, b

    (a, b), ad, _ = _run(tmp_path, monkeypatch, make)
    where = {m["text"].split("Kanban ")[1].split()[0]: m["chat_id"]
             for m in ad[Platform.DISCORD].sent if "requested" in m["text"]}
    assert where == {a: KANBAN_CC, b: APOLLO}, ad[Platform.DISCORD].sent


def test_route_channel_mode_restores_logs(tmp_path, monkeypatch):
    def make(conn):
        tid = _card(conn, S_APOLLO)
        kb.complete_task(conn, tid, summary="shipped")
        return tid

    _tid, ad, _ = _run(tmp_path, monkeypatch, make, route="channel")
    assert [m["chat_id"] for m in ad[Platform.DISCORD].sent] == [LOGS]


def test_route_rules_pure():
    ch = ("discord", LOGS)
    home = hr.home_from_row(S_APOLLO, SESSIONS[S_APOLLO])
    none = hr.home_from_row(None, None)

    class T:
        priority = 150

    r = hr.route_lifecycle_line(ch, "home", "completed", {}, T(), home)
    assert (r.target, r.fallback, r.is_home) == (("discord", APOLLO), ch, True)
    assert hr.route_lifecycle_line(ch, "home", "completed", {}, T(), none).tag == "no-home"
    # Failure kinds and p>=100 needs_input blocks stay with the subscriber.
    assert hr.route_lifecycle_line(ch, "home", "crashed", {}, T(), home).target is None
    assert hr.route_lifecycle_line(ch, "home", "blocked", {"kind": "needs_input"}, T(), home).target is None
    assert hr.route_lifecycle_line(None, "home", "completed", {}, T(), home).target is None


# --- per-destination fold ---------------------------------------------------


def test_fold_is_per_destination(tmp_path, monkeypatch):
    clock = [1000.0]

    def make(conn):
        ids = []
        for s in (S_APOLLO, S_APOLLO, S_APOLLO, S_CC, None):
            t = _card(conn, s)
            kb.complete_task(conn, t, summary="x")
            ids.append(t)
        return ids

    ids, ad, runner = _run(tmp_path, monkeypatch, make, digest=900, home_digest=120, clock=clock)
    d = ad[Platform.DISCORD]
    assert d.sent == []
    clock[0] += 120
    asyncio.run(runner._kanban_lifecycle_digest.flush(clock[0]))
    by_chat = {m["chat_id"]: m["text"] for m in d.sent}
    assert set(by_chat) == {APOLLO, KANBAN_CC}, "home batches due at 120 s, #logs still held"
    assert "3 transition(s)" in by_chat[APOLLO], "one digest for 3 lines in one home"
    clock[0] += 900
    asyncio.run(runner._kanban_lifecycle_digest.flush(clock[0]))
    logs = [m for m in d.sent if m["chat_id"] == LOGS]
    assert len(logs) == 1 and ids[4] in logs[0]["text"] and "[no-home]" in logs[0]["text"]
    assert all(ids[i] not in logs[0]["text"] for i in range(4)), "homed lines never reach #logs"


def test_folded_home_batch_unreachable_moves_to_logs_tagged():
    from gateway.kanban_lifecycle_digest import LifecycleDigest

    ad = Adapter(fail_chats={APOLLO})
    dg = LifecycleDigest()
    fb = (("discord", LOGS), ad, 0)
    for i in range(2):
        dg.add(("discord", APOLLO), ad, f"✔ [default] @w Kanban t_{i} done — x", 60, 0.0, fallback=fb)
    assert asyncio.run(dg.flush(61.0)) == 0
    assert asyncio.run(dg.flush(61.0)) == 1
    assert [m["chat_id"] for m in ad.sent] == [LOGS]
    assert ad.sent[0]["text"].count("[home-unreachable:forbidden]") == 2


# --- fixture: #logs 17:25-17:28 on 2026-10-02, re-rendered with destinations ----

TODAY = [  # (kind, card, home session) as read from kanban.db task_events, 17:23:19-17:28:39 PT
    ("completed", "t_8c473fdc", S_APOLLO),
    ("completed", "t_d53c4063", "operator:apollo"),
    ("review_requested", "t_4ce57048", S_APOLLO),
    ("review_requested", "t_a9a90c55", S_CC),
    ("completed", "t_4853212d", S_APOLLO),
    ("review_requested", "t_dcc4ed08", S_APOLLO),
    ("blocked", "t_180d564a", S_APOLLO),
    ("review_requested", "t_b3d01abe", S_APOLLO),
    ("review_requested", "t_86cce89e", S_APOLLO),
    ("blocked", "t_1e039fab", S_APOLLO),
    ("review_requested", "t_784d5a55", S_APOLLO),
    ("review_requested", "t_df8b9c98", S_APOLLO),
]


def test_fixture_todays_logs_lines_rerendered_with_destinations():
    ch = ("discord", LOGS)

    class T:
        priority = 50

    dest = {}
    for kind, card, sid in TODAY:
        r = hr.route_lifecycle_line(ch, "home", kind, {}, T(), hr.home_from_row(sid, SESSIONS.get(sid)))
        dest[card] = (r.target[1], r.tag)
    assert dest["t_d53c4063"] == (LOGS, "no-home")
    assert dest["t_a9a90c55"] == (KANBAN_CC, "")
    assert {c for c, (chat, _) in dest.items() if chat == APOLLO} == {
        c for _, c, s in TODAY if s == S_APOLLO}
    assert sum(1 for chat, _ in dest.values() if chat == LOGS) == 1, "#logs keeps only the homeless line"


# --- rehome verb --------------------------------------------------------------


def test_rehome_stamps_session_and_subscribes_its_chat(tmp_path, monkeypatch, capsys):
    from hermes_cli import kanban as kc

    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "rh.db"))
    kb.init_db()
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="orphan", assignee="w", session_id=None)
    assert kc._cmd_rehome(argparse.Namespace(task_id=tid, session=S_CC)) == 0
    with kb.connect_closing() as conn:
        assert kb.get_task(conn, tid).session_id == S_CC
        subs = kbn.list_notify_subs(conn, tid)
    assert [(s["platform"], s["chat_id"]) for s in subs] == [("discord", KANBAN_CC)]
    assert "Re-homed" in capsys.readouterr().out


def test_rehome_refuses_a_session_without_a_chat(tmp_path, monkeypatch, capsys):
    from hermes_cli import kanban as kc

    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "rh2.db"))
    kb.init_db()
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="orphan", assignee="w", session_id=None)
    assert kc._cmd_rehome(argparse.Namespace(task_id=tid, session=S_NOCHAN)) == 1
    assert "no-channel" in capsys.readouterr().err
    with kb.connect_closing() as conn:
        assert kb.get_task(conn, tid).session_id is None


def test_rehome_is_a_home_guarded_verb():
    from hermes_cli import kanban as kc

    assert "rehome" in kc._HOME_GUARDED_ACTIONS
    parser = argparse.ArgumentParser()
    kc.build_parser(parser.add_subparsers(dest="cmd"))
    ns = parser.parse_args(["kanban", "rehome", "t_1", "--session", "s"])
    assert (ns.task_id, ns.session) == ("t_1", "s")
