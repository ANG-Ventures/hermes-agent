"""The chat_type invariant belongs at the CHOKE POINT, not at each caller.

#682 fixed the two READERS. #684 fixed two WRITERS — and that was a per-caller
placement bug: `add_notify_sub` has FOUR non-test callers, and the guard only
covered two.

Measured on the live runtime immediately after #684 deployed:

    add_notify_sub(raw 'channel')              -> stored as 'channel'   BAD
    subscribe_calling_session(env 'channel')   -> stored as 'group'     ok

So the CLI (`hermes kanban notify-subscribe --chat-type channel`) and the
dashboard plugin could still persist a row that cannot match its own chat's
routing entry -> identity-less wake -> phantom session replying into the user's
channel at the config default.

`add_notify_sub` is the single layer every writer shares. An invariant placed
there cannot be bypassed by a call site added later, which is exactly the
failure mode a per-caller guard has.

Non-Discord platforms are untouched: `canonical_chat_type` is a no-op for them,
and Teams / Telegram / HomeAssistant own `channel` as a real type.
"""
import ast
import sqlite3
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb

ROOT = Path(__file__).resolve().parents[2]


def _conn(tmp_path):
    """A real, migrated board DB in a temp dir — never the live fleet DBs."""
    conn = kb.connect(tmp_path / "kanban.db")
    conn.execute(
        "INSERT OR IGNORE INTO tasks (id, title, status) VALUES (?, ?, ?)",
        ("t_probe", "probe", "ready"),
    )
    conn.commit()
    return conn


def _stored_chat_type(conn, task_id):
    row = conn.execute(
        "SELECT chat_type FROM kanban_notify_subs WHERE task_id = ?", (task_id,)
    ).fetchone()
    return row[0] if row else None


@pytest.mark.parametrize("given", ["channel", "group"])
def test_discord_guild_row_is_always_stored_canonically(tmp_path, given):
    """🔴 THE REGRESSION: the raw envelope spelling must never reach the DB."""
    conn = _conn(tmp_path)
    try:
        kb.add_notify_sub(
            conn, task_id="t_probe", platform="discord",
            chat_id="1514857406025306212", chat_type=given,
            user_id="117431298246705156",
        )
        assert _stored_chat_type(conn, "t_probe") == "group", (
            f"a Discord guild sub given chat_type={given!r} was persisted "
            "uncanonicalized — it cannot match its own chat's routing entry "
            "and will mint a phantom session (2026-09-12)"
        )
    finally:
        conn.close()


@pytest.mark.parametrize("platform", ["teams", "telegram", "homeassistant"])
def test_platforms_that_own_channel_keep_it(tmp_path, platform):
    """Negative control: a guard that rewrote every platform re-lanes three."""
    conn = _conn(tmp_path)
    try:
        kb.add_notify_sub(
            conn, task_id="t_probe", platform=platform,
            chat_id="C1", chat_type="channel", user_id="u1",
        )
        assert _stored_chat_type(conn, "t_probe") == "channel", (
            f"{platform} owns 'channel' as a real chat type — rewriting it "
            "silently moves that platform's sessions to a different lane"
        )
    finally:
        conn.close()


def test_dm_is_never_collapsed(tmp_path):
    """Negative control: dm keys on a wholly different shape than group."""
    conn = _conn(tmp_path)
    try:
        kb.add_notify_sub(
            conn, task_id="t_probe", platform="discord",
            chat_id="D1", chat_type="dm", user_id="u1",
        )
        assert _stored_chat_type(conn, "t_probe") == "dm"
    finally:
        conn.close()


def test_unset_chat_type_still_defaults_to_dm(tmp_path):
    """Negative control: the pre-existing default must be preserved."""
    conn = _conn(tmp_path)
    try:
        kb.add_notify_sub(
            conn, task_id="t_probe", platform="discord",
            chat_id="D1", chat_type=None, user_id="u1",
        )
        assert _stored_chat_type(conn, "t_probe") == "dm"
    finally:
        conn.close()


def test_guard_is_at_the_choke_point_not_only_at_callers():
    """LINT: the invariant must live in add_notify_sub itself.

    A per-caller guard is blind to every call site added later — which is how
    two of the four callers shipped uncovered in #684. If someone moves this
    back out to the callers, this fails.
    """
    source = (ROOT / "hermes_cli/kanban_db.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    fn = next(
        (n for n in ast.walk(tree)
         if isinstance(n, ast.FunctionDef) and n.name == "add_notify_sub"),
        None,
    )
    assert fn is not None, "add_notify_sub moved — re-point this lint"
    body = ast.get_source_segment(source, fn) or ""
    assert "canonical_chat_type" in body, (
        "add_notify_sub must canonicalize chat_type itself — it is the one "
        "layer every writer shares; a guard at the callers leaves the CLI and "
        "the dashboard plugin able to persist a phantom-minting row"
    )
