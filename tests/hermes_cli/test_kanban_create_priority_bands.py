"""`kanban create` default priority bands (hermes-home t_645c6b17, spec 2026-11-02 Q1 A): a human's bare create
lands at human_default 100 (p2), above every machine minter; a minter (--created-by <id>, or a cron/worker/script
process) keeps 0; an explicit --priority always wins; KANBAN_PRIORITY_BANDS=off restores 0 (I4)."""
from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from hermes_cli import kanban as kc
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_parser as kp


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv("KANBAN_PRIORITY_BANDS", raising=False)
    kb.init_db()
    return home


def _create_priority(*argv) -> int:
    root = argparse.ArgumentParser(prog="hermes")
    kc.build_parser(root.add_subparsers())
    assert kc._cmd_create(root.parse_args(["kanban", "create", *argv, "--home", "operator"])) == 0
    with kbc.connect_closing() as conn:
        return next(t.priority for t in kb.list_tasks(conn) if t.title == argv[0])


@pytest.mark.parametrize("origin,want", [("hand", 100), ("gateway", 100), ("cron", 0), ("worker", 0),
                                         ("script", 0)])
def test_human_bare_create_lands_at_human_default(kanban_home, monkeypatch, origin, want):
    monkeypatch.setattr(kb, "classify_create_origin", lambda: origin)
    assert _create_priority("bare card") == want


def test_minter_and_explicit_priority_keep_their_value(kanban_home, monkeypatch):
    monkeypatch.setattr(kb, "classify_create_origin", lambda: "hand")
    assert _create_priority("minted", "--created-by", "pager-owner-card") == 0
    assert _create_priority("explicit", "--priority", "40") == 40
    assert _create_priority("explicit zero", "--priority", "0") == 0      # an explicit 0 is not the default
    assert _create_priority("tier", "--priority", "p1") == 200


def test_kill_switch_restores_zero(kanban_home, monkeypatch):
    monkeypatch.setattr(kb, "classify_create_origin", lambda: "hand")
    monkeypatch.setenv("KANBAN_PRIORITY_BANDS", "off")
    assert _create_priority("bare card") == 0
    assert kp.create_default_priority("user", "hand") == 0


def test_create_help_documents_one_scale(capsys):
    root = argparse.ArgumentParser(prog="hermes")
    kc.build_parser(root.add_subparsers())
    with pytest.raises(SystemExit):
        root.parse_args(["kanban", "create", "--help"])
    out = " ".join(capsys.readouterr().out.split())
    assert "higher = first" in out and "human_default 100" in out and "pager_unowned 40" in out


# Live band (t_febbdb61, ruling:t_febbdb61-live-band-scope): a person's/operator's bare create lands at the
# median priority of the last 24 h of spawns (floor 100); machine minters keep their band.
def _seed_spawns(prios, *, age_s=60):
    import time
    with kbc.connect_closing() as conn:
        for i, p in enumerate(prios):
            tid = kb.create_task(conn, title=f"spawned-{i}", assignee="w", priority=p)
            kb._append_event(conn, tid, "spawned", {"pid": 1})
            conn.execute("UPDATE task_events SET created_at=? WHERE task_id=? AND kind='spawned'",
                         (int(time.time()) - age_s, tid))
        conn.commit()


def test_live_band_lifts_a_human_bare_create_to_the_spawn_median(kanban_home, monkeypatch):
    monkeypatch.setattr(kb, "classify_create_origin", lambda: "hand")
    _seed_spawns([0, 100, 170, 300, 400])                     # median 170
    assert _create_priority("bare card") == 170
    assert _create_priority("explicit", "--priority", "5") == 5
    assert _create_priority("minted", "--created-by", "pager-owner-card") == 0


def test_live_band_never_lifts_a_minter_process(kanban_home, monkeypatch):
    _seed_spawns([300, 330, 410])
    for origin in ("cron", "worker", "script"):
        monkeypatch.setattr(kb, "classify_create_origin", lambda o=origin: o)
        assert _create_priority(f"bare {origin}") == 0


def test_live_band_floor_window_and_kill_switch(kanban_home, monkeypatch):
    monkeypatch.setattr(kb, "classify_create_origin", lambda: "hand")
    _seed_spawns([0, 10, 20])                                  # median 10 -> floor 100
    _seed_spawns([900, 900, 900, 900], age_s=2 * 86400)        # outside the 24 h window
    assert _create_priority("floored") == 100
    _seed_spawns([400, 400, 400, 400, 400])
    assert _create_priority("lifted") == 400
    monkeypatch.setenv("KANBAN_LIVE_BAND", "off")
    assert _create_priority("switched off") == 100


def test_live_band_operator_profiles_only(kanban_home, monkeypatch):
    _seed_spawns([170, 170, 170])
    with kbc.connect_closing() as conn:
        assert kp.live_band_default(conn, "apollo", "worker") == 170
        assert kp.live_band_default(conn, "daedalus-opus", "worker") == 170
        assert kp.live_band_default(conn, "apollo", "cron") is None
        assert kp.live_band_default(conn, "landing-queue", "script") is None
        assert kp.live_band_default(conn, "user", "worker") is None


def test_kanban_create_tool_default_uses_live_band(kanban_home, monkeypatch):
    from tools import kanban_tools as kt
    _seed_spawns([170, 170, 170])
    monkeypatch.setattr(kb, "classify_create_origin", lambda: "worker")
    with kbc.connect_closing() as conn:
        monkeypatch.setattr(kt, "_persisted_identity", lambda: "daedalus-opus")
        assert kt._tool_default_priority(conn) == 170
        monkeypatch.setattr(kt, "_persisted_identity", lambda: "landing-queue")
        assert kt._tool_default_priority(conn) == 0


@pytest.mark.parametrize("author", ["default", "daedalus-opus"])
def test_live_band_contextvar_cron_keeps_machine_default(kanban_home, monkeypatch, author):
    from gateway.session_context import set_cron_session, clear_cron_session
    from tools import kanban_tools as kt
    for name in ("HERMES_CRON_SESSION", "HERMES_CRON_JOB_ID"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(kb, "classify_create_origin", lambda: "gateway")
    monkeypatch.setattr(kt, "_persisted_identity", lambda: author)
    _seed_spawns([170, 170, 170])
    token = set_cron_session()
    try:
        with kbc.connect_closing() as conn:
            assert kp.live_band_default(conn, author) is None
            assert kt._tool_default_priority(conn) == 0
        assert _create_priority("cron card", "--created-by", author) == 0
        assert _create_priority("explicit cron", "--created-by", author, "--priority", "5") == 5
    finally:
        clear_cron_session(token)


def test_live_band_inferred_root_cli_alias(kanban_home, monkeypatch):
    from gateway.session_context import set_cron_session, clear_cron_session
    monkeypatch.setattr(kc, "_profile_author", lambda: "default")
    monkeypatch.setattr(kb, "classify_create_origin", lambda: "script")
    _seed_spawns([170, 170, 170])
    assert _create_priority("root filing") == 170
    with kbc.connect_closing() as conn:
        assert next(t.created_by for t in kb.list_tasks(conn) if t.title == "root filing") == "user"
    token = set_cron_session()
    try:
        assert _create_priority("scheduled maintenance") == 0
    finally:
        clear_cron_session(token)
    assert _create_priority("root explicit", "--priority", "5") == 5
    monkeypatch.setenv("KANBAN_LIVE_BAND", "off")
    assert _create_priority("root disabled") == 0


@pytest.mark.parametrize("profile", ["apollo", "daedalus", "daedalus-opus"])
def test_live_band_bare_cli_create_under_operator_profile(kanban_home, monkeypatch, profile):
    # Prism 99f2e8531865: argparse's --created-by default "user" must not mask an allowlisted operator profile.
    monkeypatch.setattr(kc, "_profile_author", lambda: profile)
    monkeypatch.setattr(kb, "classify_create_origin", lambda: "worker")
    _seed_spawns([170, 170, 170])
    assert _create_priority(f"operator filing {profile}") == 170
    with kbc.connect_closing() as conn:
        assert next(t.created_by for t in kb.list_tasks(conn)
                    if t.title == f"operator filing {profile}") == "user"
    monkeypatch.setattr(kc, "_profile_author", lambda: "landing-queue")
    assert _create_priority("landing queue sweep") == 0


def test_live_band_default_gateway_alias_preserves_author(kanban_home, monkeypatch):
    from gateway.session_context import set_session_vars, restore_session_vars
    from tools import kanban_tools as kt
    monkeypatch.setattr(kb, "classify_create_origin", lambda: "gateway")
    monkeypatch.setattr(kt, "_persisted_identity", lambda: "default")
    _seed_spawns([170, 170, 170])
    tokens = set_session_vars(platform="discord", profile="default", cron_session="")
    try:
        with kbc.connect_closing() as conn:
            assert kp.live_band_default(conn, "default") == 170
            assert kt._tool_default_priority(conn) == 170
            assert kt._persisted_identity() == "default"
            monkeypatch.setenv("KANBAN_LIVE_BAND", "off")
            assert kt._tool_default_priority(conn) == 0
        assert _create_priority("alias off", "--created-by", "default") == 0
    finally:
        restore_session_vars(tokens)
